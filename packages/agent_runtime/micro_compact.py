"""In-run micro-compaction for stale, large non-RAG tool observations.

Follows Claude Code's ``microcompact`` strategy:
- Under normal context pressure, do nothing so provider KV-cache prefixes stay
  100% untouched.
- When pressure reaches the high-water mark (default >= 75% of available input
  budget), collapse only *stale* non-RAG tool results (older than the most
  recent ``keep_recent_tool_messages`` tool observations and larger than
  ``min_chars_to_compact``) into compact JSON stubs preserving status,
  error_code, artifact_ref (if present), original size, and a short preview.
- Never mutates system prompts, user tasks, assistant turns, or RAG evidence
  messages (whose short citation IDs are bound to ``rag_evidence_registry``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from packages.agent_runtime.evidence_registry import RAG_EVIDENCE_TOOL_IDENTITIES
from packages.model_gateway.contracts import ModelMessage

DEFAULT_KEEP_RECENT_TOOL_MESSAGES = 3
DEFAULT_MIN_CHARS_TO_COMPACT = 800
DEFAULT_PREVIEW_CHARS = 160
DEFAULT_PRESSURE_TRIGGER_RATIO = 0.80
DEFAULT_MIN_ROUNDS_BETWEEN_COMPACTIONS = 2


@dataclass(frozen=True, slots=True)
class MicroCompactReport:
    compacted_count: int
    saved_chars: int
    triggered: bool
    skipped_reason: str | None = None


def _is_already_compacted(content: str) -> bool:
    try:
        parsed = json.loads(content)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("micro_compacted") is True


def _compact_tool_stub(message: ModelMessage, *, preview_chars: int) -> str:
    raw = message.content or ""
    status: Any = None
    error_code: Any = None
    artifact_ref: Any = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            status = parsed.get("status")
            error_code = parsed.get("error_code")
            artifact_ref = parsed.get("artifact_ref")
    except ValueError:
        parsed = None

    stub: dict[str, Any] = {
        "micro_compacted": True,
        "tool": message.name,
        "original_chars": len(raw),
        "preview": raw[:preview_chars],
    }
    if status is not None:
        stub["status"] = status
    if error_code is not None:
        stub["error_code"] = error_code
    if artifact_ref is not None:
        stub["artifact_ref"] = artifact_ref
    return json.dumps(stub, ensure_ascii=False, separators=(",", ":"))


def micro_compact_tool_observations(
    messages: Sequence[ModelMessage],
    *,
    estimated_input_tokens: int | None = None,
    available_input_tokens: int | None = None,
    pressure_trigger_ratio: float = DEFAULT_PRESSURE_TRIGGER_RATIO,
    keep_recent_tool_messages: int = DEFAULT_KEEP_RECENT_TOOL_MESSAGES,
    min_chars_to_compact: int = DEFAULT_MIN_CHARS_TO_COMPACT,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
    current_round: int | None = None,
    last_compacted_round: int | None = None,
    min_rounds_between_compactions: int = DEFAULT_MIN_ROUNDS_BETWEEN_COMPACTIONS,
) -> tuple[list[ModelMessage], MicroCompactReport]:
    """Batch-fold stale oversized non-RAG tool observations with hysteresis & cooldown."""
    if (
        estimated_input_tokens is not None
        and available_input_tokens is not None
        and available_input_tokens > 0
        and (estimated_input_tokens / available_input_tokens) < pressure_trigger_ratio
    ):
        return list(messages), MicroCompactReport(
            compacted_count=0,
            saved_chars=0,
            triggered=False,
            skipped_reason="BELOW_HIGH_WATER",
        )

    if (
        current_round is not None
        and last_compacted_round is not None
        and (current_round - last_compacted_round) < min_rounds_between_compactions
    ):
        return list(messages), MicroCompactReport(
            compacted_count=0,
            saved_chars=0,
            triggered=False,
            skipped_reason="COOLDOWN_ACTIVE",
        )

    tool_indices = [
        idx
        for idx, msg in enumerate(messages)
        if msg.role == "tool" and msg.name not in RAG_EVIDENCE_TOOL_IDENTITIES
    ]
    if len(tool_indices) <= keep_recent_tool_messages:
        return list(messages), MicroCompactReport(
            compacted_count=0,
            saved_chars=0,
            triggered=True,
            skipped_reason="NOTHING_TO_COMPACT",
        )

    protected = set(
        tool_indices[-keep_recent_tool_messages:] if keep_recent_tool_messages > 0 else []
    )
    out: list[ModelMessage] = []
    compacted_count = 0
    saved_chars = 0

    for idx, msg in enumerate(messages):
        if (
            msg.role == "tool"
            and msg.name not in RAG_EVIDENCE_TOOL_IDENTITIES
            and idx not in protected
            and isinstance(msg.content, str)
            and len(msg.content) >= min_chars_to_compact
            and not _is_already_compacted(msg.content)
        ):
            stub = _compact_tool_stub(msg, preview_chars=preview_chars)
            if len(stub) < len(msg.content):
                saved_chars += len(msg.content) - len(stub)
                compacted_count += 1
                out.append(replace(msg, content=stub))
                continue
        out.append(msg)

    return out, MicroCompactReport(
        compacted_count=compacted_count,
        saved_chars=saved_chars,
        triggered=True,
        skipped_reason=None if compacted_count > 0 else "NOTHING_TO_COMPACT",
    )


__all__ = [
    "DEFAULT_KEEP_RECENT_TOOL_MESSAGES",
    "DEFAULT_MIN_CHARS_TO_COMPACT",
    "DEFAULT_PRESSURE_TRIGGER_RATIO",
    "DEFAULT_PREVIEW_CHARS",
    "MicroCompactReport",
    "micro_compact_tool_observations",
]
