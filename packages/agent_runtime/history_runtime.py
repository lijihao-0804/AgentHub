"""Bridge the frozen thread sources to the token-based history planner."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from packages.agent_runtime.context_budget import ContextBudgetPolicy
from packages.agent_runtime.history_planner import (
    DecisionReason,
    HistoryCategory,
    HistoryPlan,
    HistoryScope,
    HistorySourceGroup,
    Insufficiency,
    InsufficiencyCode,
    SummaryEligibility,
    plan_history,
)
from packages.agent_runtime.work_layer import (
    PLAIN_TEXT_PREFIX_V1,
    PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON,
    PlainTextPrefixProofStatus,
    ThreadConversation,
    ThreadTurnContext,
)
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelMessage, ModelToolDefinition


@dataclass(frozen=True, slots=True)
class PlainTextPrefixSource:
    """One source in the exact older prefix selected by the server policy."""

    source_id: str
    turn_id: UUID
    run_id: UUID
    sequence: int
    revision_sha256: str
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class PlainTextPrefix:
    """B's selector for the contiguous eligible sources in this frozen view."""

    policy: str
    sources: tuple[PlainTextPrefixSource, ...]
    stopped_before_source_id: str | None
    stop_reason: str | None

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(source.source_id for source in self.sources)

    @property
    def estimated_tokens(self) -> int:
        return sum(source.estimated_tokens for source in self.sources)


@dataclass(frozen=True, slots=True)
class PlannedHistory:
    plan: HistoryPlan
    messages: tuple[ModelMessage, ...]
    memory_messages: tuple[ModelMessage, ...]
    exchange_ids: tuple[str, ...]
    plain_text_prefix: PlainTextPrefix | None = None


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
    summary_source_policy: str | None = None,
) -> PlannedHistory:
    """Measure all sources; optionally return a blocked plan for job scheduling.

    A blocked plan carries no sendable messages. The default retains the M3
    fail-closed behavior for all existing callers.
    """
    if not isinstance(return_blocked_plan, bool):
        raise TypeError("return_blocked_plan must be a bool")
    if summary_source_policy not in {None, PLAIN_TEXT_PREFIX_V1}:
        raise ValueError(f"summary_source_policy must be {PLAIN_TEXT_PREFIX_V1} or None")
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
    prefix_sources: list[PlainTextPrefixSource] = []
    prefix_open = summary_source_policy is not None
    prefix_stopped_before: str | None = None
    prefix_stop_reason: str | None = None
    expected_prefix_sequence = 1
    recent_start = max(len(conversation.turns) - 2, 0)
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
        source_tokens = sum(policy._estimate_message(item) for item in pair)
        category = HistoryCategory.KEEP_EXACT
        eligibility = SummaryEligibility.UNKNOWN
        eligibility_reason = getattr(source, "summary_eligibility_reason", "NOT_CLASSIFIED")
        if index >= recent_start:
            category = HistoryCategory.RECENT
            if summary_source_policy is not None:
                eligibility = SummaryEligibility.INELIGIBLE
                eligibility_reason = "PLAIN_TEXT_PREFIX_V1_RECENT_EXCHANGE_RETAINED"
        elif summary_source_policy is None:
            category = HistoryCategory.KEEP_EXACT
        elif not prefix_open:
            category = HistoryCategory.KEEP_EXACT
            eligibility = SummaryEligibility.INELIGIBLE
            eligibility_reason = "PLAIN_TEXT_PREFIX_V1_CONTIGUITY_CLOSED"
        else:
            proof = turn.plain_text_prefix_v1_proof
            if source.sequence != expected_prefix_sequence:
                prefix_open = False
                prefix_stopped_before = source.source_id
                prefix_stop_reason = "PLAIN_TEXT_PREFIX_V1_SOURCE_SEQUENCE_GAP"
            elif proof is None:
                prefix_open = False
                prefix_stopped_before = source.source_id
                prefix_stop_reason = "PLAIN_TEXT_PREFIX_V1_PERSISTED_PROOF_MISSING"
            elif proof.status is PlainTextPrefixProofStatus.UNPROVEN:
                prefix_open = False
                prefix_stopped_before = source.source_id
                prefix_stop_reason = proof.reason
            elif proof.status is PlainTextPrefixProofStatus.INELIGIBLE:
                prefix_open = False
                prefix_stopped_before = source.source_id
                prefix_stop_reason = proof.reason
            elif proof.status is not PlainTextPrefixProofStatus.ELIGIBLE:
                prefix_open = False
                prefix_stopped_before = source.source_id
                prefix_stop_reason = "PLAIN_TEXT_PREFIX_V1_PROOF_STATUS_INVALID"
            elif proof.reason != PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON:
                prefix_open = False
                prefix_stopped_before = source.source_id
                prefix_stop_reason = "PLAIN_TEXT_PREFIX_V1_PROOF_POLICY_MISMATCH"
            elif not _is_plain_text_exchange(turn):
                prefix_open = False
                prefix_stopped_before = source.source_id
                prefix_stop_reason = "PLAIN_TEXT_PREFIX_V1_PROOF_CONTRADICTED_BY_TURN"
            else:
                category = HistoryCategory.OPTIONAL
                eligibility = SummaryEligibility.ELIGIBLE
                eligibility_reason = proof.reason
                prefix_sources.append(
                    PlainTextPrefixSource(
                        source_id=source.source_id,
                        turn_id=source.turn_id,
                        run_id=source.run_id,
                        sequence=source.sequence,
                        revision_sha256=source.revision_sha256,
                        estimated_tokens=source_tokens,
                    )
                )
                expected_prefix_sequence += 1
            if category is not HistoryCategory.OPTIONAL:
                eligibility = SummaryEligibility.INELIGIBLE
                eligibility_reason = (
                    prefix_stop_reason
                    if prefix_stop_reason is not None
                    else eligibility_reason
                )
        add(
            source.source_id,
            category,
            source.sequence,
            source.revision_sha256,
            source_tokens,
            eligibility_reason,
            eligibility,
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
    plan = _block_uncovered_optional_history_drops(plan)
    if not plan.ready_for_dispatch:
        if return_blocked_plan:
            return PlannedHistory(
                plan=plan,
                messages=(),
                memory_messages=(),
                exchange_ids=(),
                plain_text_prefix=_plain_text_prefix_result(
                    summary_source_policy,
                    prefix_sources,
                    prefix_stopped_before,
                    prefix_stop_reason,
                ),
            )
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
        plan=plan,
        messages=history,
        memory_messages=memory,
        exchange_ids=exchange_ids,
        plain_text_prefix=_plain_text_prefix_result(
            summary_source_policy,
            prefix_sources,
            prefix_stopped_before,
            prefix_stop_reason,
        ),
    )


def _is_plain_text_exchange(turn: ThreadTurnContext) -> bool:
    """Reject context evidence that contradicts a server eligibility proof."""

    if (
        turn.artifact_ids
        or turn.artifact_refs
        or turn.evidence_registry is not None
        or turn.source_knowledge_snapshots
        or turn.source_wiki_publications
        or not turn.messages
        or not isinstance(turn.user_input, str)
        or not turn.user_input.strip()
        or not isinstance(turn.final_output, str)
        or not turn.final_output.strip()
        or "\x00" in turn.user_input
        or "\x00" in turn.final_output
        or not isinstance(turn.messages, tuple)
    ):
        return False
    return (
        len(turn.messages) == 2
        and turn.messages[0].role == "user"
        and turn.messages[0].content == turn.user_input
        and not turn.messages[0].tool_calls
        and turn.messages[0].tool_call_id is None
        and turn.messages[1].role == "assistant"
        and turn.messages[1].content == turn.final_output
        and not turn.messages[1].tool_calls
        and turn.messages[1].tool_call_id is None
    )


def _plain_text_prefix_result(
    policy: str | None,
    sources: list[PlainTextPrefixSource],
    stopped_before_source_id: str | None,
    stop_reason: str | None,
) -> PlainTextPrefix | None:
    if policy is None:
        return None
    if not sources and stop_reason is None:
        stop_reason = "PLAIN_TEXT_PREFIX_V1_NO_ELIGIBLE_OLD_EXCHANGE"
    return PlainTextPrefix(
        policy=policy,
        sources=tuple(sources),
        stopped_before_source_id=stopped_before_source_id,
        stop_reason=stop_reason,
    )


def _block_uncovered_optional_history_drops(plan: HistoryPlan) -> HistoryPlan:
    """Refuse to omit optional history without a retained verified summary.

    ``OPTIONAL`` allows the planner to omit a group. Historical groups are
    replaceable only when an exact verified summary covering that group is
    retained. This also protects groups whose source eligibility is unknown
    or ineligible if a caller categorizes them as optional.
    """

    retained_summary_ids = {
        decision.group_id
        for decision in plan.decisions
        if (
            decision.category is HistoryCategory.VERIFIED_SUMMARY
            and decision.retained
        )
    }
    uncovered = tuple(
        decision.group_id
        for decision in plan.decisions
        if (
            decision.category is HistoryCategory.OPTIONAL
            and decision.sequence > 0
            and not decision.retained
            and not (
                decision.reason is DecisionReason.DROPPED_COVERED_BY_SUMMARY
                and decision.covered_by_summary_id in retained_summary_ids
            )
        )
    )
    if not uncovered:
        return plan
    blocking = Insufficiency(
        code=InsufficiencyCode.VERIFIED_SUMMARY_NOT_RETAINED,
        blocking=True,
        message=(
            "Optional historical sources would be dropped before a verified summary "
            "replaces them."
        ),
    )
    return replace(
        plan,
        insufficiencies=plan.insufficiencies + (blocking,),
        ready_for_dispatch=False,
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
