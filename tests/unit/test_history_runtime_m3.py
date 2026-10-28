"""The runtime bridge counts uncropped source messages with the M2 estimator."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.context_budget import ContextBudgetPolicy
from packages.agent_runtime.history_checkpoint import verify_history_checkpoint_binding
from packages.agent_runtime.history_planner import (
    DecisionReason,
    HistoryCategory,
    HistoryScope,
    HistorySourceGroup,
    InsufficiencyCode,
    PlanPressure,
    PlanStatus,
    SummaryEligibility,
    plan_history,
)
from packages.agent_runtime.history_runtime import (
    _block_uncovered_optional_history_drops,
    _Counter,
    _namespace_tool_ids,
    plan_history_view,
)
from packages.agent_runtime.work_layer import (
    PLAIN_TEXT_PREFIX_V1,
    PLAIN_TEXT_PREFIX_V1_TRANSCRIPT_UNAVAILABLE,
    PlainTextPrefixProof,
    PlainTextPrefixProofStatus,
    ThreadConversation,
    ThreadTurnContext,
)
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from packages.threads.context import (
    SqlAlchemyThreadContextProvider,
    _plain_text_prefix_v1_proof,
)
from tests.unit.test_context_policy_m2_runtime import Chars, _freeze, _profile


@pytest.mark.asyncio
async def test_old_checkpoint_remains_usable_but_summary_requires_a_binding():
    def unexpected_database_access():
        raise AssertionError("missing binding is decided before database access")

    run = SimpleNamespace(thread_id=None)
    assert (
        await verify_history_checkpoint_binding(
            unexpected_database_access, run=run, state={"runtime": {}}
        )
        is None
    )
    with pytest.raises(AgentHubError) as error:
        await verify_history_checkpoint_binding(
            unexpected_database_access,
            run=run,
            state={"runtime": {"summary_policy": {"enabled": True}}},
        )
    assert error.value.code == "AGENT_HISTORY_SNAPSHOT_INVALID"


def _conversation(count: int):
    workspace_id, thread_id = uuid4(), uuid4()
    sources, turns = [], []
    for index in range(count):
        turn_id, run_id = uuid4(), uuid4()
        sources.append(
            SimpleNamespace(
                source_id=f"thread-turn:{turn_id}",
                turn_id=turn_id,
                run_id=run_id,
                sequence=index + 1,
                revision_sha256=f"revision-{index}",
            )
        )
        turns.append(
            ThreadTurnContext(
                "q" * 950,
                "a" * 950,
                turn_id=turn_id,
                run_id=run_id,
                sequence=index + 1,
                messages=(
                    ModelMessage("user", "q" * 950),
                    ModelMessage("assistant", "a" * 950),
                ),
            )
        )
    manifest = SimpleNamespace(
        status="COMPLETE",
        selected_source_count=count,
        available_eligible_source_count=count,
        selected_sources=sources,
        scope=SimpleNamespace(
            workspace_id=workspace_id, thread_id=thread_id, before_sequence=count + 1
        ),
    )
    return ThreadConversation(turns=tuple(turns), turns_available=count, source_manifest=manifest)


def _sized_conversation(sizes, *, eligible_prefix: int = 0):
    workspace_id, thread_id = uuid4(), uuid4()
    sources, turns = [], []
    for index, target_tokens in enumerate(sizes):
        text_tokens = target_tokens - 8
        assert text_tokens >= 2
        user_input = "q" * (text_tokens // 2)
        final_output = "a" * (text_tokens - len(user_input))
        turn_id, run_id = uuid4(), uuid4()
        sources.append(
            SimpleNamespace(
                source_id=f"thread-turn:{turn_id}",
                turn_id=turn_id,
                run_id=run_id,
                sequence=index + 1,
                revision_sha256=f"revision-{index}",
            )
        )
        turns.append(
            ThreadTurnContext(
                user_input,
                final_output,
                turn_id=turn_id,
                run_id=run_id,
                sequence=index + 1,
                messages=(
                    ModelMessage("user", user_input),
                    ModelMessage("assistant", final_output),
                ),
                plain_text_prefix_v1_proof=(
                    PlainTextPrefixProof(
                        PlainTextPrefixProofStatus.ELIGIBLE,
                        "PLAIN_TEXT_PREFIX_V1_SIMPLE_COMPLETED_EXCHANGE",
                    )
                    if index < eligible_prefix
                    else None
                ),
            )
        )
    manifest = SimpleNamespace(
        status="COMPLETE",
        selected_source_count=len(sizes),
        available_eligible_source_count=len(sizes),
        selected_sources=sources,
        scope=SimpleNamespace(
            workspace_id=workspace_id, thread_id=thread_id, before_sequence=len(sizes) + 1
        ),
    )
    return ThreadConversation(
        turns=tuple(turns), turns_available=len(sizes), source_manifest=manifest
    )


def test_runtime_counts_all_history_before_legacy_ten_turn_cut():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    result = plan_history_view(
        policy=policy,
        conversation=_conversation(12),
        mandatory_messages=(ModelMessage("user", "question"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
    )
    assert result.plan.pre_trim_pressure is PlanPressure.SOFT
    assert result.plan.pre_trim_tokens > result.plan.thresholds.soft_tokens
    assert len(result.messages) == 24


def test_runtime_refuses_a_partially_measured_history():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    conversation = _conversation(2)
    conversation.source_manifest.available_eligible_source_count = 3
    with pytest.raises(AgentHubError) as error:
        plan_history_view(
            policy=policy,
            conversation=conversation,
            mandatory_messages=(ModelMessage("user", "question"),),
            memory_messages=(),
            tools=(),
            permission_snapshot="user-with-run-permission",
        )
    assert error.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"


def test_blocked_plan_can_be_examined_without_creating_a_sendable_view():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    arguments = dict(
        policy=policy,
        conversation=_conversation(18),
        mandatory_messages=(ModelMessage("user", "question"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
    )
    with pytest.raises(AgentHubError) as error:
        plan_history_view(**arguments)
    assert error.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
    blocked = plan_history_view(**arguments, return_blocked_plan=True)
    assert not blocked.plan.ready_for_dispatch
    assert blocked.plan.pre_trim_pressure is PlanPressure.HARD
    assert blocked.messages == blocked.memory_messages == blocked.exchange_ids == ()


def test_old_default_under_pressure_without_summary_returns_a_blocked_plan():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    conversation = _sized_conversation([5065, 10000, 10000, 4000, 50, 50])

    with pytest.raises(AgentHubError) as error:
        plan_history_view(
            policy=policy,
            conversation=conversation,
            mandatory_messages=(ModelMessage("user", "current input"),),
            memory_messages=(),
            tools=(),
            permission_snapshot="user-with-run-permission",
        )
    assert error.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"

    blocked = plan_history_view(
        policy=policy,
        conversation=conversation,
        mandatory_messages=(ModelMessage("user", "current input"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
        return_blocked_plan=True,
    )
    assert blocked.plan.status is PlanStatus.BLOCKED
    assert not blocked.plan.ready_for_dispatch
    assert blocked.plain_text_prefix is None
    assert blocked.messages == blocked.memory_messages == blocked.exchange_ids == ()
    assert any(
        item.code is InsufficiencyCode.RECENT_SOURCE_NOT_RETAINED and item.blocking
        for item in blocked.plan.insufficiencies
    )


def test_eligible_optional_drop_without_summary_fails_closed_and_keeps_prefix():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    conversation = _sized_conversation(
        [5065, 10000, 10000, 4000, 50, 50],
        eligible_prefix=4,
    )

    blocked = plan_history_view(
        policy=policy,
        conversation=conversation,
        mandatory_messages=(ModelMessage("user", "current input"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
        summary_source_policy=PLAIN_TEXT_PREFIX_V1,
        return_blocked_plan=True,
    )

    dropped = next(
        item
        for item in blocked.plan.decisions
        if item.category is HistoryCategory.OPTIONAL and not item.retained
    )
    assert dropped.reason is DecisionReason.DROPPED_SHARED_BUDGET
    assert blocked.plan.status is PlanStatus.BLOCKED
    assert not blocked.plan.ready_for_dispatch
    assert any(
        item.code is InsufficiencyCode.VERIFIED_SUMMARY_NOT_RETAINED and item.blocking
        for item in blocked.plan.insufficiencies
    )
    assert blocked.messages == blocked.memory_messages == blocked.exchange_ids == ()
    assert blocked.plain_text_prefix is not None
    assert blocked.plain_text_prefix.source_ids == tuple(
        source.source_id for source in conversation.source_manifest.selected_sources[:4]
    )
    assert blocked.plain_text_prefix.estimated_tokens == 29065
    assert all(
        item.retained
        for item in blocked.plan.decisions
        if item.category is HistoryCategory.RECENT
    )
    assert next(
        item for item in blocked.plan.decisions if item.group_id == "runtime:mandatory"
    ).retained

    with pytest.raises(AgentHubError) as error:
        plan_history_view(
            policy=policy,
            conversation=conversation,
            mandatory_messages=(ModelMessage("user", "current input"),),
            memory_messages=(),
            tools=(),
            permission_snapshot="user-with-run-permission",
            summary_source_policy=PLAIN_TEXT_PREFIX_V1,
        )
    assert error.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"


def test_plain_text_prefix_is_opt_in_and_default_plan_is_unchanged():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    result = plan_history_view(
        policy=policy,
        conversation=_conversation(6),
        mandatory_messages=(ModelMessage("user", "current input"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
    )

    assert result.plain_text_prefix is None
    assert [item.category for item in result.plan.decisions[1:5]] == [
        HistoryCategory.KEEP_EXACT,
        HistoryCategory.KEEP_EXACT,
        HistoryCategory.KEEP_EXACT,
        HistoryCategory.KEEP_EXACT,
    ]
    assert all(
        item.summary_eligibility is SummaryEligibility.UNKNOWN
        for item in result.plan.decisions[1:]
    )


def test_plain_text_prefix_is_contiguous_and_preserves_recent_turns_and_current_input():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    conversation = _conversation(7)
    statuses = (
        PlainTextPrefixProofStatus.ELIGIBLE,
        PlainTextPrefixProofStatus.ELIGIBLE,
        PlainTextPrefixProofStatus.INELIGIBLE,
        PlainTextPrefixProofStatus.ELIGIBLE,
        PlainTextPrefixProofStatus.ELIGIBLE,
        PlainTextPrefixProofStatus.ELIGIBLE,
        PlainTextPrefixProofStatus.ELIGIBLE,
    )
    conversation = replace(
        conversation,
        turns=tuple(
            replace(
                turn,
                plain_text_prefix_v1_proof=PlainTextPrefixProof(
                    status,
                    (
                        "PLAIN_TEXT_PREFIX_V1_SIMPLE_COMPLETED_EXCHANGE"
                        if status is PlainTextPrefixProofStatus.ELIGIBLE
                        else "PLAIN_TEXT_PREFIX_V1_TOOL_ACTIVITY"
                    ),
                ),
            )
            for turn, status in zip(conversation.turns, statuses, strict=True)
        ),
    )

    result = plan_history_view(
        policy=policy,
        conversation=conversation,
        mandatory_messages=(ModelMessage("user", "current input"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
        summary_source_policy=PLAIN_TEXT_PREFIX_V1,
    )

    prefix = result.plain_text_prefix
    assert prefix is not None
    assert prefix.source_ids == tuple(
        source.source_id for source in conversation.source_manifest.selected_sources[:2]
    )
    assert prefix.stopped_before_source_id == (
        conversation.source_manifest.selected_sources[2].source_id
    )
    assert prefix.stop_reason == "PLAIN_TEXT_PREFIX_V1_TOOL_ACTIVITY"
    decisions = {item.group_id: item for item in result.plan.decisions}
    assert decisions["runtime:mandatory"].category is HistoryCategory.MANDATORY
    assert decisions["runtime:mandatory"].retained
    assert decisions["runtime:mandatory"].tokens == (
        policy._protocol_overhead("request_overhead_tokens")
        + policy._estimate_message(ModelMessage("user", "current input"))
    )
    assert [
        decisions[source.source_id].category
        for source in conversation.source_manifest.selected_sources
    ] == [
        HistoryCategory.OPTIONAL,
        HistoryCategory.OPTIONAL,
        HistoryCategory.KEEP_EXACT,
        HistoryCategory.KEEP_EXACT,
        HistoryCategory.KEEP_EXACT,
        HistoryCategory.RECENT,
        HistoryCategory.RECENT,
    ]
    expected_recent_messages = tuple(
        message
        for turn in conversation.turns[-2:]
        for message in turn.messages
    )
    assert result.messages[-4:] == expected_recent_messages


def test_plain_text_prefix_does_not_bridge_an_unknown_sequence_gap():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    conversation = _conversation(5)
    sources = list(conversation.source_manifest.selected_sources)
    turns = list(conversation.turns)
    for index, sequence in enumerate((1, 4, 5, 6, 7)):
        sources[index].sequence = sequence
        turns[index] = replace(
            turns[index],
            sequence=sequence,
            plain_text_prefix_v1_proof=PlainTextPrefixProof(
                PlainTextPrefixProofStatus.ELIGIBLE,
                "PLAIN_TEXT_PREFIX_V1_SIMPLE_COMPLETED_EXCHANGE",
            ),
        )
    conversation.source_manifest.selected_sources = tuple(sources)
    conversation.source_manifest.scope.before_sequence = 8
    conversation = replace(conversation, turns=tuple(turns))

    result = plan_history_view(
        policy=policy,
        conversation=conversation,
        mandatory_messages=(ModelMessage("user", "current input"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
        summary_source_policy=PLAIN_TEXT_PREFIX_V1,
    )

    prefix = result.plain_text_prefix
    assert prefix is not None
    assert prefix.source_ids == (sources[0].source_id,)
    assert prefix.stopped_before_source_id == sources[1].source_id
    assert prefix.stop_reason == "PLAIN_TEXT_PREFIX_V1_SOURCE_SEQUENCE_GAP"


def test_missing_server_proof_stops_prefix_even_with_transcript():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    conversation = _conversation(4)

    result = plan_history_view(
        policy=policy,
        conversation=conversation,
        mandatory_messages=(ModelMessage("user", "current input"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
        summary_source_policy=PLAIN_TEXT_PREFIX_V1,
    )

    prefix = result.plain_text_prefix
    assert prefix is not None
    assert prefix.sources == ()
    assert prefix.stopped_before_source_id == (
        conversation.source_manifest.selected_sources[0].source_id
    )
    assert prefix.stop_reason == "PLAIN_TEXT_PREFIX_V1_PERSISTED_PROOF_MISSING"
    assert all(
        item.summary_eligibility is not SummaryEligibility.ELIGIBLE
        for item in result.plan.decisions
    )


def test_empty_transcript_cannot_become_eligible_from_a_supplied_proof():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    conversation = _conversation(4)
    first_turn = replace(
        conversation.turns[0],
        messages=(),
        plain_text_prefix_v1_proof=PlainTextPrefixProof(
            PlainTextPrefixProofStatus.ELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_SIMPLE_COMPLETED_EXCHANGE",
        ),
    )
    conversation = replace(conversation, turns=(first_turn, *conversation.turns[1:]))

    result = plan_history_view(
        policy=policy,
        conversation=conversation,
        mandatory_messages=(ModelMessage("user", "current input"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
        summary_source_policy=PLAIN_TEXT_PREFIX_V1,
    )

    assert result.plain_text_prefix is not None
    assert result.plain_text_prefix.sources == ()
    assert result.plain_text_prefix.stop_reason == (
        "PLAIN_TEXT_PREFIX_V1_PROOF_CONTRADICTED_BY_TURN"
    )


def test_empty_transcript_is_unproven_when_side_effect_queries_are_clear():
    run_id = uuid4()
    turn = SimpleNamespace(user_input="question", final_output="answer")
    run = SimpleNamespace(
        id=run_id,
        status="SUCCEEDED",
        tool_call_count=0,
        failure_code=None,
        waiting_approval_id=None,
        thread_history_evidence_registry=None,
        effective_knowledge_snapshots=(),
        effective_wiki_publications=(),
        final_output="answer",
    )

    proof = _plain_text_prefix_v1_proof(
        turn,
        run,
        messages=(),
        artifacts=(),
        approval_run_ids=frozenset(),
        action_run_ids=frozenset(),
    )

    assert proof.status is PlainTextPrefixProofStatus.UNPROVEN
    assert proof.reason == PLAIN_TEXT_PREFIX_V1_TRANSCRIPT_UNAVAILABLE


def test_dropped_optional_historical_source_requires_a_verified_replacement():
    scope = HistoryScope("workspace", "thread", "permission-snapshot")
    groups = (
        HistorySourceGroup(
            group_id="runtime:mandatory",
            scope=scope,
            revision="runtime-v1",
            sequence=0,
            watermark=2,
            exchange_id="runtime:mandatory",
            original_ref="runtime:mandatory",
            count_version=1,
            category=HistoryCategory.MANDATORY,
        ),
        HistorySourceGroup(
            group_id="thread-turn:unproven",
            scope=scope,
            revision="source-v1",
            sequence=1,
            watermark=2,
            exchange_id="thread-turn:unproven",
            original_ref="thread-turn:unproven",
            count_version=1,
            category=HistoryCategory.OPTIONAL,
            summary_eligibility=SummaryEligibility.INELIGIBLE,
        ),
    )
    raw_plan = plan_history(
        groups,
        scope=scope,
        frozen_watermark=2,
        counter=_Counter({"runtime:mandatory": 80, "thread-turn:unproven": 40}),
        b_work_tokens=100,
    )
    assert raw_plan.ready_for_dispatch
    assert not next(
        item for item in raw_plan.decisions if item.group_id == "thread-turn:unproven"
    ).retained

    guarded = _block_uncovered_optional_history_drops(raw_plan)

    assert not guarded.ready_for_dispatch
    assert any(
        item.code is InsufficiencyCode.VERIFIED_SUMMARY_NOT_RETAINED and item.blocking
        for item in guarded.insufficiencies
    )


@pytest.mark.asyncio
async def test_default_history_evidence_is_not_queried_without_opt_in():
    class ExplodingSession:
        def begin_nested(self):
            raise AssertionError("default history must not open evidence queries")

        async def scalars(self, *_args, **_kwargs):
            raise AssertionError("default history must not query evidence tables")

    approvals, actions = await SqlAlchemyThreadContextProvider._plain_text_prefix_blockers(
        ExplodingSession(),
        workspace_id=uuid4(),
        run_ids=(uuid4(),),
        enabled=False,
    )

    assert approvals is actions is None


@pytest.mark.parametrize(
    ("overrides", "approval", "action", "artifact", "expected_status", "expected_reason"),
    [
        ({}, False, False, False, PlainTextPrefixProofStatus.ELIGIBLE, None),
        (
            {"tool_call_count": 1},
            False,
            False,
            False,
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_TOOL_ACTIVITY",
        ),
        (
            {"failure_code": "UNKNOWN_OUTCOME"},
            False,
            False,
            False,
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_FAILURE_OR_UNKNOWN_OUTCOME",
        ),
        (
            {},
            True,
            False,
            False,
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_APPROVAL_ACTIVITY",
        ),
        (
            {},
            False,
            True,
            False,
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_ACTION_ACTIVITY",
        ),
        (
            {"thread_history_evidence_registry": {}},
            False,
            False,
            False,
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_EVIDENCE_PRESENT",
        ),
        (
            {"effective_knowledge_snapshots": [{"knowledge_base_id": str(uuid4())}]},
            False,
            False,
            False,
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_SOURCE_EVIDENCE_PRESENT",
        ),
        (
            {},
            False,
            False,
            True,
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_ARTIFACT_PRESENT",
        ),
        (
            {"status": "RUNNING"},
            False,
            False,
            False,
            PlainTextPrefixProofStatus.UNPROVEN,
            "PLAIN_TEXT_PREFIX_V1_COMPLETION_STATUS_UNAVAILABLE",
        ),
        (
            {"tool_call_count": None},
            False,
            False,
            False,
            PlainTextPrefixProofStatus.UNPROVEN,
            "PLAIN_TEXT_PREFIX_V1_TOOL_COUNT_UNAVAILABLE",
        ),
    ],
)
def test_plain_text_prefix_proof_uses_persisted_facts_with_complete_transcript(
    overrides,
    approval,
    action,
    artifact,
    expected_status,
    expected_reason,
):
    run_id = uuid4()
    turn = SimpleNamespace(user_input="question", final_output="answer")
    run = SimpleNamespace(
        **{
            "id": run_id,
            "status": "SUCCEEDED",
            "tool_call_count": 0,
            "failure_code": None,
            "waiting_approval_id": None,
            "thread_history_evidence_registry": None,
            "effective_knowledge_snapshots": [],
            "effective_wiki_publications": [],
            "final_output": "answer",
            **overrides,
        }
    )
    messages = (ModelMessage("user", "question"), ModelMessage("assistant", "answer"))

    proof = _plain_text_prefix_v1_proof(
        turn,
        run,
        messages=messages,
        artifacts=(object(),) if artifact else (),
        approval_run_ids=frozenset({run_id}) if approval else frozenset(),
        action_run_ids=frozenset({run_id}) if action else frozenset(),
    )

    assert proof.status is expected_status
    if expected_reason is None:
        assert proof.reason == "PLAIN_TEXT_PREFIX_V1_SIMPLE_COMPLETED_EXCHANGE"
    else:
        assert proof.reason == expected_reason


def test_protocol_ids_are_isolated_between_different_historical_turns():
    messages = (
        ModelMessage("user", "question"),
        ModelMessage("assistant", tool_calls=(ModelToolCall("lookup", {}, "call-1"),)),
        ModelMessage("tool", "result", name="lookup", tool_call_id="call-1"),
        ModelMessage("assistant", "answer"),
    )
    first = _namespace_tool_ids(uuid4().hex, messages)
    second = _namespace_tool_ids(uuid4().hex, messages)
    first_id, second_id = (
        first[1].tool_calls[0].provider_tool_call_id,
        second[1].tool_calls[0].provider_tool_call_id,
    )
    assert first_id != second_id
    assert first[2].tool_call_id == first_id and second[2].tool_call_id == second_id
