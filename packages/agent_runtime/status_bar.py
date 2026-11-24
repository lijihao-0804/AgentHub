"""Code-maintained tail <agent_status> bar for KV-friendly runtime awareness.

Instead of asking the model to recount steps, tool call quotas, repeated calls,
or remaining context budget from long histories, runtime code maintains these
counters incrementally and injects a compact `<agent_status>` block at the very
tail of the request (where R = 0, preserving all prior KV-cache blocks).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from packages.model_gateway.contracts import ModelMessage

STATUS_TAG_OPEN = "<agent_status>"
STATUS_TAG_CLOSE = "</agent_status>"


@dataclass(frozen=True, slots=True)
class AgentStatusSnapshot:
    model_round: int
    max_steps: int
    tool_call_count: int
    max_tool_calls: int
    tool_usage_counts: Mapping[str, int] = field(default_factory=dict)
    tool_failure_counts: Mapping[str, int] = field(default_factory=dict)
    max_identical_calls: int | None = None
    max_observed_identical_calls: int = 0
    estimated_input_tokens: int | None = None
    available_input_tokens: int | None = None
    pending_approvals: int = 0


def format_agent_status(snapshot: AgentStatusSnapshot) -> str:
    """Render a deterministic, compact XML <agent_status> block."""
    remaining_steps = max(0, snapshot.max_steps - snapshot.model_round)
    remaining_tools = max(0, snapshot.max_tool_calls - snapshot.tool_call_count)
    lines = [
        STATUS_TAG_OPEN,
        f"- Steps: {snapshot.model_round}/{snapshot.max_steps} (remaining: {remaining_steps})",
        f"- Tool calls: {snapshot.tool_call_count}/{snapshot.max_tool_calls} "
        f"(remaining: {remaining_tools})",
    ]
    if snapshot.tool_usage_counts:
        parts = []
        for name in sorted(snapshot.tool_usage_counts):
            total = snapshot.tool_usage_counts[name]
            failed = snapshot.tool_failure_counts.get(name, 0)
            parts.append(f"{name}={total}" + (f" ({failed} failed)" if failed else ""))
        lines.append("- Per-tool counts: " + ", ".join(parts))
    if (
        snapshot.max_identical_calls is not None
        and snapshot.max_observed_identical_calls > 0
        and snapshot.max_observed_identical_calls >= snapshot.max_identical_calls
    ):
        lines.append(
            f"- Repeat guard: identical call limit reached "
            f"({snapshot.max_observed_identical_calls}/{snapshot.max_identical_calls}); "
            "change arguments or conclude."
        )
    if (
        snapshot.estimated_input_tokens is not None
        and snapshot.available_input_tokens is not None
        and snapshot.available_input_tokens > 0
    ):
        pct = round((snapshot.estimated_input_tokens / snapshot.available_input_tokens) * 100.0, 1)
        lines.append(
            f"- Context budget: {snapshot.estimated_input_tokens}/"
            f"{snapshot.available_input_tokens} tokens ({pct}%)"
        )
    if snapshot.pending_approvals > 0:
        lines.append(f"- Pending human approvals: {snapshot.pending_approvals}")
    lines.append(STATUS_TAG_CLOSE)
    return "\n".join(lines)


def attach_tail_status_bar(
    messages: Sequence[ModelMessage],
    snapshot: AgentStatusSnapshot,
) -> list[ModelMessage]:
    """Append or replace the tail <agent_status> block without touching the prefix.

    If the last message is already a standalone runtime status user message, it
    is replaced in place (R = 0 tail replacement). Otherwise a new tail user
    message is appended so every prior message remains byte-identical for KV
    prefix caching.
    """
    status_text = format_agent_status(snapshot)
    result = list(messages)
    if (
        result
        and result[-1].role == "user"
        and isinstance(result[-1].content, str)
        and result[-1].content.startswith(STATUS_TAG_OPEN)
        and result[-1].content.endswith(STATUS_TAG_CLOSE)
    ):
        result[-1] = replace(result[-1], content=status_text)
    else:
        result.append(ModelMessage(role="user", content=status_text))
    return result


__all__ = [
    "AgentStatusSnapshot",
    "STATUS_TAG_CLOSE",
    "STATUS_TAG_OPEN",
    "attach_tail_status_bar",
    "format_agent_status",
]
