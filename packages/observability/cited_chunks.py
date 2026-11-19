"""Resolve cited RAW search handles into identifiers without returning trace content."""

from __future__ import annotations

import re
from collections.abc import Mapping
from uuid import UUID

from psycopg import Error as CheckpointDatabaseError
from sqlalchemy import exists, select, tuple_

from packages.agent_runtime.adapters.langgraph.checkpoint import checkpoint_thread_id
from packages.agent_runtime.history_evidence import validate_history_evidence
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.models import (
    DocumentChunk,
    DocumentRevision,
    KnowledgeSnapshot,
    KnowledgeSnapshotItem,
    RevisionLifecycleStatus,
)
from packages.model_gateway.contracts import ModelMessage
from packages.tools.projections import evidence_refs
from packages.wiki.models import WikiSourceRevocation

_HANDLE = re.compile(r"\b(?:T[0-9a-f]{20})?R[0-9]{5}E[0-9]{1,3}\b")
_REQUIRED = {"workspace_read", "agent_run", "knowledge_run", "tool_run"}


async def cited_chunk_refs(session, context, run, adapter):
    if (
        not _REQUIRED.issubset(context.permissions)
        or run.status != "SUCCEEDED"
        or run.workspace_id != UUID(context.workspace_id)
        or not run.thread_turn_id
        or not run.effective_knowledge_snapshots
        or not run.final_output
    ):
        return []
    handles = list(dict.fromkeys(_HANDLE.findall(run.final_output)))
    if not handles or len(handles) > 100:
        return []
    try:
        async with adapter.checkpointer() as cp:
            item = await cp.aget_tuple(adapter.config_for_run(run.workspace_id, run.id))
    except (CheckpointDatabaseError, OSError):
        # Evidence is optional; the durable tool-event response is still available.
        return []
    if item is None or not isinstance(item.checkpoint, Mapping):
        return []
    identity = item.config.get("configurable", {})
    if (
        identity.get("thread_id") != checkpoint_thread_id(run.workspace_id, run.id)
        or identity.get("checkpoint_ns") != ""
        or not identity.get("checkpoint_id")
        or identity.get("checkpoint_id") != item.checkpoint.get("id")
    ):
        return []
    state = item.checkpoint.get("channel_values", {})
    if not isinstance(state, Mapping):
        return []
    messages = state.get("messages") or []
    if (
        str(state.get("run_id")) != str(run.id)
        or str(state.get("agent_version_id")) != str(run.agent_version_id)
        or state.get("final_output") != run.final_output
        or state.get("effective_knowledge_snapshots") != run.effective_knowledge_snapshots
        or not isinstance(messages, (list, tuple))
        or not messages
        or not all(isinstance(message, ModelMessage) for message in messages)
        or messages[-1].role != "assistant"
        or messages[-1].content != run.final_output
        or any(m.name == "query_wiki" for m in messages)
    ):
        return []
    try:
        verified = validate_history_evidence(
            messages,
            state.get("rag_evidence_registry", {}),
            run.effective_knowledge_snapshots,
            run.effective_knowledge_snapshots,
            [],
            [],
            context.permissions,
        )
    except AgentHubError:
        return []
    refs = {}
    for handle in handles:
        if handle.startswith("T") and not handle.startswith(f"T{run.id.hex[:20]}R"):
            continue
        entry = verified.get(handle)
        if entry is None or entry["record"].get("origin") not in (None, "RAW"):
            continue
        safe = evidence_refs({"evidence_refs": [entry["reference"]]})
        if safe:
            refs[handle] = safe[0]
    if not refs:
        return []
    # Match the current source, immutable snapshot membership AND its frozen hash.
    # Select IDs only; no chunk text, document metadata or checkpoint records leave this service.
    fields = (
        DocumentChunk.knowledge_base_id,
        KnowledgeSnapshotItem.snapshot_id,
        DocumentChunk.document_revision_id,
        DocumentChunk.chunk_id,
    )

    def key(ref):
        return (
            UUID(ref["knowledge_base_id"]),
            UUID(ref["snapshot_id"]),
            UUID(ref["document_revision_id"]),
            ref["chunk_id"],
        )

    try:
        source_hashes = [
            (
                UUID(b["knowledge_base_id"]),
                UUID(b["snapshot_id"]),
                b.get("snapshot_hash") or b.get("snapshot_content_hash"),
            )
            for b in run.effective_knowledge_snapshots
        ]
    except (KeyError, ValueError, TypeError, AttributeError):
        return []
    rows = await session.execute(
        select(*fields)
        .select_from(DocumentChunk)
        .join(
            KnowledgeSnapshotItem,
            (KnowledgeSnapshotItem.workspace_id == DocumentChunk.workspace_id)
            & (KnowledgeSnapshotItem.knowledge_base_id == DocumentChunk.knowledge_base_id)
            & (KnowledgeSnapshotItem.document_id == DocumentChunk.document_id)
            & (KnowledgeSnapshotItem.document_revision_id == DocumentChunk.document_revision_id),
        )
        .join(
            KnowledgeSnapshot,
            (KnowledgeSnapshot.workspace_id == KnowledgeSnapshotItem.workspace_id)
            & (KnowledgeSnapshot.knowledge_base_id == KnowledgeSnapshotItem.knowledge_base_id)
            & (KnowledgeSnapshot.id == KnowledgeSnapshotItem.snapshot_id),
        )
        .join(
            DocumentRevision,
            (DocumentRevision.workspace_id == DocumentChunk.workspace_id)
            & (DocumentRevision.id == DocumentChunk.document_revision_id),
        )
        .where(
            DocumentChunk.workspace_id == run.workspace_id,
            DocumentRevision.lifecycle_status == RevisionLifecycleStatus.ACTIVE,
            tuple_(
                KnowledgeSnapshot.knowledge_base_id,
                KnowledgeSnapshot.id,
                KnowledgeSnapshot.content_hash,
            ).in_(source_hashes),
            tuple_(*fields).in_([key(ref) for ref in refs.values()]),
            ~exists().where(
                WikiSourceRevocation.workspace_id == DocumentChunk.workspace_id,
                WikiSourceRevocation.knowledge_base_id == DocumentChunk.knowledge_base_id,
                WikiSourceRevocation.document_revision_id == DocumentChunk.document_revision_id,
            ),
        )
    )
    present = set(rows)
    return [{"handle": handle, **ref} for handle, ref in refs.items() if key(ref) in present]
