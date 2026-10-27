"""Bridge the frozen thread sources to the token-based history planner."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from packages.agent_runtime.context_budget import ContextBudgetPolicy
from packages.agent_runtime.history_planner import (
    HistoryCategory,
    HistoryPlan,
    HistoryScope,
    HistorySourceGroup,
    SummaryEligibility,
    plan_history,
)
from packages.agent_runtime.work_layer import ThreadConversation
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelMessage, ModelToolDefinition


@dataclass(frozen=True, slots=True)
class PlannedHistory:
    plan: HistoryPlan
    messages: tuple[ModelMessage, ...]
    memory_messages: tuple[ModelMessage, ...]
    exchange_ids: tuple[str, ...]


class _Counter:
    counter_id = "approx_language_v1"
    counter_version = 1

    def __init__(self, values: dict[str, int]) -> None:
        self.values = values

    def count(self, group: HistorySourceGroup) -> int:
        return self.values[group.group_id]


def plan_history_view(
    *,
    policy: ContextBudgetPolicy,
    conversation: ThreadConversation,
    mandatory_messages: Sequence[ModelMessage],
    memory_messages: Sequence[ModelMessage],
    tools: Sequence[ModelToolDefinition],
    permission_snapshot: str,
    return_blocked_plan: bool = False,
) -> PlannedHistory:
    """Measure all sources; optionally return a blocked plan for job scheduling.

    A blocked plan carries no sendable messages. The default retains the M3
    fail-closed behavior for all existing callers.
    """
    if not isinstance(return_blocked_plan, bool):
        raise TypeError("return_blocked_plan must be a bool")
    manifest = conversation.source_manifest
    if manifest is None or manifest.status != "COMPLETE":
        raise AgentHubError(
            "AGENT_HISTORY_CONTEXT_INSUFFICIENT", "Thread history is incomplete.", 422
        )
    if manifest.selected_source_count != manifest.available_eligible_source_count:
        raise AgentHubError(
            "AGENT_HISTORY_CONTEXT_INSUFFICIENT", "Thread history is incomplete.", 422
        )
    if len(manifest.selected_sources) != len(conversation.turns):
        raise AgentHubError("AGENT_HISTORY_SNAPSHOT_INVALID", "The frozen history is invalid.", 422)
    if policy.context_policy is None:
        raise AgentHubError(
            "AGENT_CONTEXT_POLICY_INVALID", "The frozen context policy is invalid.", 422
        )
    scope = HistoryScope(
        workspace_id=str(manifest.scope.workspace_id),
        thread_id=str(manifest.scope.thread_id),
        permission_snapshot=permission_snapshot,
    )
    watermark = manifest.scope.before_sequence
    groups: list[HistorySourceGroup] = []
    counts: dict[str, int] = {}

    def add(
        group_id: str,
        category: HistoryCategory,
        sequence: int,
        revision: str,
        tokens: int,
        eligibility_reason: str = "NOT_APPLICABLE",
        eligibility: SummaryEligibility = SummaryEligibility.INELIGIBLE,
    ):
        groups.append(
            HistorySourceGroup(
                group_id=group_id,
                scope=scope,
                revision=revision,
                sequence=sequence,
                watermark=watermark,
                exchange_id=group_id,
                original_ref=group_id,
                count_version=1,
                category=category,
                summary_eligibility=eligibility,
                summary_eligibility_reason=eligibility_reason,
            )
        )
        counts[group_id] = tokens

    mandatory_tokens = policy._protocol_overhead("request_overhead_tokens")
    mandatory_tokens += sum(policy._estimate_message(item) for item in mandatory_messages)
    mandatory_tokens += sum(policy._estimate_tool(item).total for item in tools)
    add("runtime:mandatory", HistoryCategory.MANDATORY, 0, "runtime-v1", mandatory_tokens)
    for index, message in enumerate(memory_messages):
        add(
            f"runtime:memory:{index}",
            HistoryCategory.OPTIONAL,
            0,
            "memory-v1",
            policy._estimate_message(message),
        )
    pairs: dict[str, tuple[ModelMessage, ...]] = {}
    for index, (source, turn) in enumerate(
        zip(manifest.selected_sources, conversation.turns, strict=True)
    ):
        if (
            turn.turn_id != source.turn_id
            or turn.run_id != source.run_id
            or turn.sequence != source.sequence
        ):
            raise AgentHubError(
                "AGENT_HISTORY_SNAPSHOT_INVALID", "The frozen history is invalid.", 422
            )
        text = turn.final_output
        if turn.artifact_refs:
            text = "\n".join((text, *turn.artifact_refs))
        pair = (
            _namespace_tool_ids(turn.turn_id.hex, turn.messages)
            if turn.messages
            else (
                ModelMessage("user", turn.user_input),
                ModelMessage("assistant", text),
            )
        )
        pairs[source.source_id] = pair
        category = (
            HistoryCategory.RECENT
            if index >= len(conversation.turns) - 2
            else HistoryCategory.KEEP_EXACT
        )
        add(
            source.source_id,
            category,
            source.sequence,
            source.revision_sha256,
            sum(policy._estimate_message(item) for item in pair),
            getattr(source, "summary_eligibility_reason", "NOT_CLASSIFIED"),
            SummaryEligibility.UNKNOWN,
        )
    plan = plan_history(
        groups,
        scope=scope,
        frozen_watermark=watermark,
        counter=_Counter(counts),
        b_work_tokens=policy.context_policy["input_limit_tokens"],
        expected_counter_id=policy.context_policy["counter"]["id"],
        expected_counter_version=policy.context_policy["counter"]["version"],
    )
    if not plan.ready_for_dispatch:
        if return_blocked_plan:
            return PlannedHistory(plan=plan, messages=(), memory_messages=(), exchange_ids=())
        raise AgentHubError(
            "AGENT_HISTORY_CONTEXT_INSUFFICIENT", "Thread history does not fit safely.", 422
        )
    selected = set(plan.retained_group_ids)
    history = tuple(
        message
        for source in manifest.selected_sources
        if source.source_id in selected
        for message in pairs[source.source_id]
    )
    memory = tuple(
        message
        for index, message in enumerate(memory_messages)
        if f"runtime:memory:{index}" in selected
    )
    exchange_ids = tuple(
        source.source_id
        for source in manifest.selected_sources
        if source.source_id in selected
        for _ in pairs[source.source_id]
    )
    return PlannedHistory(
        plan=plan, messages=history, memory_messages=memory, exchange_ids=exchange_ids
    )


def _namespace_tool_ids(
    turn_id: str, messages: tuple[ModelMessage, ...]
) -> tuple[ModelMessage, ...]:
    """Avoid collisions between provider protocol IDs reused in different turns."""
    mapping: dict[str, str] = {}
    result: list[ModelMessage] = []
    for message in messages:
        if message.tool_calls:
            calls = []
            for call in message.tool_calls:
                original = call.provider_tool_call_id
                if original is None or original in mapping:
                    raise AgentHubError(
                        "AGENT_HISTORY_SNAPSHOT_INVALID", "The frozen history is invalid.", 422
                    )
                replacement = f"history-{turn_id}-{len(mapping)}"
                mapping[original] = replacement
                calls.append(replace(call, provider_tool_call_id=replacement))
            result.append(replace(message, tool_calls=tuple(calls)))
        elif message.role == "tool":
            if message.tool_call_id not in mapping:
                raise AgentHubError(
                    "AGENT_HISTORY_SNAPSHOT_INVALID", "The frozen history is invalid.", 422
                )
            result.append(replace(message, tool_call_id=mapping[message.tool_call_id]))
        else:
            result.append(message)
    return tuple(result)
