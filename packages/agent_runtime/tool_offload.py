"""Large tool-output offloading and paginated artifact pointer readback.

Follows the context-offloading pattern used by long-horizon agent runtimes
(Manus / Claude Code): when a non-RAG tool returns a payload larger than
``DEFAULT_OFFLOAD_THRESHOLD_BYTES``, store the full text in a run-scoped
offload store and inject a compact preview + pointer stub into the prompt.
The model can inspect arbitrary slices via the read-only ``read_tool_artifact``
tool without blowing up the context window or losing raw data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.agent_runtime.evidence_registry import RAG_EVIDENCE_TOOL_IDENTITIES
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
    ToolSourceKind,
)

READ_TOOL_ARTIFACT = "read_tool_artifact"
DEFAULT_OFFLOAD_THRESHOLD_BYTES = 8_192
DEFAULT_PREVIEW_CHARS = 600
DEFAULT_READ_LIMIT_CHARS = 2_000
MAX_READ_LIMIT_CHARS = 8_000

_READ_TOOL_ARTIFACT_REVISION_ID = UUID("00000000-0000-0000-0000-00000000a002")
_EXCLUDED_TOOLS = RAG_EVIDENCE_TOOL_IDENTITIES | frozenset({READ_TOOL_ARTIFACT})


@dataclass(frozen=True, slots=True)
class OffloadResult:
    offloaded: bool
    inline_content: str
    artifact_ref: str | None = None
    stored_entry: dict[str, Any] | None = None


def read_tool_artifact_definition() -> ToolDefinition:
    """Return the read-only builtin tool definition for reading offloaded tool outputs."""
    return ToolDefinition(
        identity=READ_TOOL_ARTIFACT,
        revision_id=_READ_TOOL_ARTIFACT_REVISION_ID,
        spec_hash="builtin:read_tool_artifact:v1",
        description=(
            "Read a character slice from an offloaded large tool output by artifact_ref. "
            "Use offset and limit to paginate through the full output without overloading context."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["artifact_ref"],
            "properties": {
                "artifact_ref": {
                    "type": "string",
                    "description": "The artifact_ref returned in an offloaded tool output stub.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "0-based character offset to start reading from.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_LIMIT_CHARS,
                    "description": "Maximum number of characters to return.",
                },
            },
        },
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        source_kind=ToolSourceKind.BUILTIN,
    )


def maybe_offload_tool_output(
    *,
    tool_name: str,
    tool_call_id: str,
    serialized_content: str,
    threshold_bytes: int = DEFAULT_OFFLOAD_THRESHOLD_BYTES,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> OffloadResult:
    """Offload an oversized non-RAG tool output into a compact pointer stub."""
    if tool_name in _EXCLUDED_TOOLS or threshold_bytes <= 0:
        return OffloadResult(offloaded=False, inline_content=serialized_content)
    encoded_len = len(serialized_content.encode("utf-8"))
    if encoded_len <= threshold_bytes:
        return OffloadResult(offloaded=False, inline_content=serialized_content)

    artifact_ref = f"offload-{tool_call_id}"
    total_lines = serialized_content.count("\n") + 1
    preview = serialized_content[:preview_chars]
    stub = {
        "offloaded": True,
        "artifact_ref": artifact_ref,
        "tool": tool_name,
        "original_bytes": encoded_len,
        "total_chars": len(serialized_content),
        "total_lines": total_lines,
        "preview": preview,
        "hint": (
            f"Full output offloaded ({encoded_len} bytes). "
            f"Call {READ_TOOL_ARTIFACT}(artifact_ref='{artifact_ref}', offset=0, limit=2000) "
            "to inspect specific slices."
        ),
    }
    stored_entry = {
        "artifact_ref": artifact_ref,
        "tool": tool_name,
        "content": serialized_content,
        "original_bytes": encoded_len,
        "total_chars": len(serialized_content),
        "total_lines": total_lines,
    }
    return OffloadResult(
        offloaded=True,
        inline_content=json.dumps(stub, ensure_ascii=False, separators=(",", ":")),
        artifact_ref=artifact_ref,
        stored_entry=stored_entry,
    )


def read_offloaded_slice(
    store: Mapping[str, Mapping[str, Any]],
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Read a bounded character slice from an offloaded tool output store."""
    artifact_ref = arguments.get("artifact_ref")
    if not isinstance(artifact_ref, str) or not artifact_ref.strip():
        return {
            "error_code": "INVALID_ARGUMENT",
            "message": "artifact_ref must be a non-empty string.",
        }
    entry = store.get(artifact_ref.strip())
    if not isinstance(entry, Mapping) or not isinstance(entry.get("content"), str):
        return {
            "error_code": "ARTIFACT_NOT_FOUND",
            "message": "The requested artifact_ref was not found.",
        }

    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", DEFAULT_READ_LIMIT_CHARS)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return {
            "error_code": "INVALID_ARGUMENT",
            "message": "offset must be a non-negative integer.",
        }
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_READ_LIMIT_CHARS
    ):
        return {
            "error_code": "INVALID_ARGUMENT",
            "message": f"limit must be an integer between 1 and {MAX_READ_LIMIT_CHARS}.",
        }

    content = entry["content"]
    total_chars = len(content)
    sliced = content[offset : offset + limit]
    next_offset = offset + len(sliced)
    return {
        "artifact_ref": artifact_ref.strip(),
        "tool": entry.get("tool"),
        "offset": offset,
        "limit": limit,
        "total_chars": total_chars,
        "has_more": next_offset < total_chars,
        "next_offset": next_offset if next_offset < total_chars else None,
        "content": sliced,
    }


__all__ = [
    "DEFAULT_OFFLOAD_THRESHOLD_BYTES",
    "DEFAULT_PREVIEW_CHARS",
    "DEFAULT_READ_LIMIT_CHARS",
    "MAX_READ_LIMIT_CHARS",
    "OffloadResult",
    "READ_TOOL_ARTIFACT",
    "maybe_offload_tool_output",
    "read_offloaded_slice",
    "read_tool_artifact_definition",
]
