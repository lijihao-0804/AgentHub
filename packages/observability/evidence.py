"""Authorized reads of safe tool events and the exact evidence they referenced."""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select

from packages.agent_runtime.events import AgentEvent, AgentEventType
from packages.agent_runtime.models import AgentRun, AgentRunEvent
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.models import DocumentChunk, KnowledgeSnapshotItem
from packages.knowledge.snapshots import KnowledgeSnapshotService

TOOL_EVENTS = ("tool.requested", "tool.started", "tool.completed", "tool.failed")


class RunEvidenceService:
    async def _run(self, session, context, run_id):
        if "workspace_read" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
        run = await session.scalar(
            select(AgentRun).where(
                AgentRun.workspace_id == UUID(context.workspace_id), AgentRun.id == run_id
            )
        )
        if run is None:
            raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
        return run

    async def tools(self, session, context, run_id):
        run = await self._run(session, context, run_id)
        rows = list(
            await session.scalars(
                select(AgentRunEvent)
                .where(
                    AgentRunEvent.workspace_id == run.workspace_id,
                    AgentRunEvent.agent_run_id == run.id,
                    AgentRunEvent.event_type.in_(TOOL_EVENTS),
                )
                .order_by(AgentRunEvent.sequence)
                .limit(201)
            )
        )
        events = []
        references = []
        for row in rows[:200]:
            try:
                event = AgentEvent(
                    sequence=row.sequence,
                    type=row.event_type,
                    request_id=row.request_id,
                    run_id=str(run.id),
                    agent_version_id=str(run.agent_version_id),
                    event_id=row.event_id,
                    step_id=row.step_id,
                    timestamp=row.occurred_at,
                    payload=row.payload,
                )
            except (TypeError, ValueError):
                continue
            events.append(event.as_dict())
            if event.type is AgentEventType.TOOL_COMPLETED:
                references.extend(json.loads(event.payload.get("evidence_refs", "[]")))
        unique = {tuple(sorted(r.items())): r for r in references}
        return {
            "events": events,
            "evidence_refs": list(unique.values()),
            "truncated": len(rows) > 200,
            "content_allowed": "knowledge_run" in context.permissions,
            "replay_complete": False,
        }

    async def content(self, session, context, run_id, *, snapshot_id, chunk_id):
        run = await self._run(session, context, run_id)
        if "knowledge_run" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "Knowledge content permission is required.", 403)
        facts = await self.tools(session, context, run_id)
        ref = next(
            (
                r
                for r in facts["evidence_refs"]
                if r["snapshot_id"] == str(snapshot_id) and r["chunk_id"] == chunk_id
            ),
            None,
        )
        binding = next(
            (
                r
                for r in run.effective_knowledge_snapshots or []
                if r.get("snapshot_id") == str(snapshot_id)
            ),
            None,
        )
        if (
            ref is None
            or binding is None
            or binding.get("knowledge_base_id") != ref["knowledge_base_id"]
        ):
            raise AgentHubError(
                "HISTORICAL_EVIDENCE_NOT_FOUND", "The recorded evidence is unavailable.", 404
            )
        snapshot = await KnowledgeSnapshotService().resolve_snapshot(
            session, context, UUID(ref["knowledge_base_id"]), snapshot_id
        )
        if binding.get("snapshot_hash") and binding["snapshot_hash"] != snapshot.content_hash:
            raise AgentHubError(
                "KNOWLEDGE_SNAPSHOT_INCONSISTENT", "The snapshot identity is inconsistent.", 409
            )
        chunk = await session.scalar(
            select(DocumentChunk)
            .join(
                KnowledgeSnapshotItem,
                (KnowledgeSnapshotItem.workspace_id == DocumentChunk.workspace_id)
                & (KnowledgeSnapshotItem.knowledge_base_id == DocumentChunk.knowledge_base_id)
                & (KnowledgeSnapshotItem.document_id == DocumentChunk.document_id)
                & (
                    KnowledgeSnapshotItem.document_revision_id == DocumentChunk.document_revision_id
                ),
            )
            .where(
                DocumentChunk.workspace_id == run.workspace_id,
                DocumentChunk.knowledge_base_id == UUID(ref["knowledge_base_id"]),
                DocumentChunk.document_revision_id == UUID(ref["document_revision_id"]),
                DocumentChunk.chunk_id == chunk_id,
                KnowledgeSnapshotItem.snapshot_id == snapshot_id,
            )
        )
        if chunk is None:
            raise AgentHubError(
                "HISTORICAL_EVIDENCE_NOT_FOUND", "The recorded evidence is unavailable.", 404
            )
        return {
            **ref,
            "snapshot_hash": snapshot.content_hash,
            "text": chunk.text[:4096],
            "truncated": len(chunk.text) > 4096,
        }
