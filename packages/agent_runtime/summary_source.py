"""Stable identity for a verified original history source across later watermarks.

This does not decide whether a source may be summarized. Callers must first
replay the M3 manifest with its original scope and authorize its contents.
"""

from __future__ import annotations

import hashlib

from packages.agent_runtime.work_layer import ThreadHistorySourceIdentity, ThreadTurnContext
from packages.core.canonical.json_hash import canonical_json_hash

SUMMARY_SOURCE_IDENTITY_VERSION = "thread-summary-source-v1"


def stable_summary_source_identity(
    source: ThreadHistorySourceIdentity, turn: ThreadTurnContext
) -> str:
    """Bind the original content and scope without binding a later Run's watermark."""
    if (
        turn.turn_id != source.turn_id
        or turn.run_id != source.run_id
        or turn.sequence != source.sequence
        or tuple(turn.artifact_ids) != source.artifact_ids
        or _text_digest(turn.user_input) != source.user_input_sha256
        or _text_digest(turn.final_output) != source.final_output_sha256
    ):
        raise ValueError("History source does not match its verified turn.")
    messages = [
        {
            "role": message.role,
            "content": message.content,
            "name": message.name,
            "tool_call_id": message.tool_call_id,
            "tool_calls": [
                {
                    "name": call.name,
                    "arguments": dict(call.arguments),
                    "provider_tool_call_id": call.provider_tool_call_id,
                }
                for call in message.tool_calls
            ],
        }
        for message in turn.messages
    ]
    return canonical_json_hash(
        {
            "version": SUMMARY_SOURCE_IDENTITY_VERSION,
            "workspace_id": str(source.workspace_id),
            "thread_id": str(source.thread_id),
            "source_id": source.source_id,
            "turn_id": str(source.turn_id),
            "run_id": str(source.run_id),
            "sequence": source.sequence,
            "user_input_sha256": source.user_input_sha256,
            "final_output_sha256": source.final_output_sha256,
            "artifacts": [item.to_manifest() for item in source.artifact_identities],
            "messages": messages,
            "artifact_refs": list(turn.artifact_refs),
            "evidence_registry": turn.evidence_registry,
            "knowledge_snapshots": list(turn.source_knowledge_snapshots),
            "wiki_publications": list(turn.source_wiki_publications),
        }
    )


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
