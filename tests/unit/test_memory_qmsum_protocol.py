"""Pure QMSum protocol tests; synthetic data never enters public quality metrics."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from benchmarks.evaluation.memory_qmsum_protocol import (
    QMSUM_QUESTION_COUNT,
    QMSumArm,
    QMSumArmRecord,
    QMSumCallMeasurements,
    QMSumCaseStatus,
    QMSumCompressedCandidate,
    QMSumFrozenQuestion,
    QMSumObservationStatus,
    QMSumOnlineProjection,
    QMSumProtocolError,
    QMSumReference,
    QMSumReviewKind,
    QMSumReviewRecord,
    QMSumReviewVerdict,
    QMSumSourceBundle,
    QMSumSourceProvenance,
    QMSumTranscriptTurn,
    assert_safe_online_payload,
    balanced_arm_orders,
    build_online_messages,
    frozen_meetings,
    lexical_reference_scores,
    online_projection,
    plan_offline,
    record_observations,
    require_b_admission,
    summarize_score_records,
    validate_online_experiment_cases,
)
from packages.agent_runtime.context_budget import (
    ContextAdmissionResult,
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextBudgetUsage,
    ContextCategory,
)
from packages.agent_runtime.summary_validation import (
    SummaryValidationResult,
    ValidatedSummary,
)
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
)


class CharacterEstimator:
    def estimate(self, text: str) -> int:
        return len(text)


class FakePolicy:
    """Deterministic zero-cost admission with a fixed per-message ceiling."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple] = []

    def admit(self, messages=(), categories=None, exchange_groups=None, tool_definitions=()):
        del categories, exchange_groups
        rendered = "".join(message.content or "" for message in messages)
        self.calls.append(tuple(message.category for message in messages))
        if self.fail_on and self.fail_on in rendered:
            raise AgentHubError("TEST_BUDGET_REJECTED", "fixture rejected", 422)
        total = sum(len(message.content or "") for message in messages)
        return ContextAdmissionResult(
            messages=tuple(message.message for message in messages),
            tool_definitions=tuple(tool_definitions),
            usage=ContextBudgetUsage(
                context_limit=10_000,
                reserved_output=100,
                available_input=9_900,
                estimated_input_before=total,
                estimated_input_after=total,
                truncated=False,
                dropped_exchange_count=0,
            ),
            message_categories=tuple(message.category for message in messages),
        )


def _question(meeting: str, domain: str, index: int, query: str) -> QMSumFrozenQuestion:
    transcript = (
        QMSumTranscriptTurn(0, "Chair", "We must keep the 20 minute limit."),
        QMSumTranscriptTurn(
            1,
            "Member",
            "Proposal: schedule a follow-up. It is not approved or executed.",
        ),
    )
    return QMSumFrozenQuestion(
        question_id=f"QMSum@83d7768:ALL/train/{meeting}.json#specific_query_list[{index}]",
        meeting_id=meeting,
        domain=domain,
        query_index=index,
        meeting_sha256="a" * 64,
        question=query,
        transcript=transcript,
    )


def _source() -> QMSumSourceBundle:
    domains = {
        "Bmr003": "Academic",
        "Buw001": "Academic",
        "TS3005d": "Product",
        "TS3006d": "Product",
        "covid_1": "Committee",
        "covid_3": "Committee",
    }
    questions = tuple(
        _question(meeting, domain, index, f"Synthetic query {meeting}/{index}")
        for meeting, domain in domains.items()
        for index in (0, 1)
    )
    references = {
        question.question_id: QMSumReference("Synthetic reference answer", ((0, 1),))
        for question in questions
    }
    return QMSumSourceBundle(
        "83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e",
        frozen_meetings(),
        questions,
        provenance=QMSumSourceProvenance.VERIFIED_FROZEN_QMSUM,
        _references=references,
    )


def _execution_plan(*, context: int = 4_096, output: int = 128):
    profile = ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        provider_credential_id=uuid4(),
        provider="offline-test",
        model="offline-test",
        temperature=Decimal("0"),
        max_tokens=output,
        timeout_seconds=Decimal("30"),
        capabilities=ModelCapabilities(max_context_tokens=context),
    )
    return ResolvedModelExecutionPlan(primary=profile)


def _real_policy(*, context: int = 4_096, retrieval: int = 4_000):
    return ContextBudgetPolicy(
        _execution_plan(context=context),
        ContextBudgetConfig(
            reserved_output_tokens=128,
            max_retrieval_tokens=retrieval,
            max_tool_result_tokens=0,
        ),
        estimator=CharacterEstimator(),
    )


def _validated_candidate(
    question_id: str,
    *,
    final_view: str = '{"status":"OK"}',
    claimed_source_tokens: int | None = None,
    validation_tokens: int | None = None,
) -> QMSumCompressedCandidate:
    candidate = ValidatedSummary(
        status="OK",
        goal=type("TextItem", (), {"text": "goal", "source_ids": ("source-1",)})(),
        decisions=(),
        supported_facts=(),
        conflicts=(),
        open_questions=(),
        completed_actions_refs=(),
        next_steps=(),
        omitted_source_ids=(),
        canonical_content='{"status":"OK"}',
        summary_estimated_tokens=8,
        final_view_text=final_view,
        final_view_estimated_tokens=(
            validation_tokens if validation_tokens is not None else len(final_view)
        ),
        covered_source_ids=("source-1",),
    )
    validation = SummaryValidationResult(
        valid=True,
        validator_version="test",
        issues=(),
        candidate=candidate,
        summary_estimated_tokens=8,
        final_view_estimated_tokens=(
            validation_tokens if validation_tokens is not None else len(final_view)
        ),
        covered_source_ids=("source-1",),
        omitted_source_ids=(),
    )
    return QMSumCompressedCandidate(
        candidate_id=f"candidate:{question_id}",
        arm=QMSumArm.C,
        validation=validation,
        source_question_ids=(question_id,),
        source_estimated_tokens=(
            claimed_source_tokens
            if claimed_source_tokens is not None
            else max(len(final_view) + 1, 1_000)
        ),
    )


def _measurements(input_tokens: int | None, output_tokens: int | None = 10):
    return QMSumCallMeasurements(
        input_tokens,
        output_tokens,
        wall_time_ms=20,
        cost_cny=Decimal("0.001"),
        cost_identity="synthetic-not-quality",
    )


def _plan(source, *, admission=None, compressed_candidates=None, seed=None):
    kwargs = {
        "admission": admission or FakePolicy(),
        "token_counter": CharacterEstimator(),
        "compressed_candidates": compressed_candidates,
    }
    if seed is not None:
        kwargs["seed"] = seed
    if compressed_candidates:
        estimator = CharacterEstimator()
        corrected = {}
        for question_id, candidate in compressed_candidates.items():
            question = next(
                question for question in source.questions if question.question_id == question_id
            )
            transcript_tokens = estimator.estimate(
                json.dumps(
                    [[turn.speaker, turn.content] for turn in question.transcript],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            corrected[question_id] = QMSumCompressedCandidate(
                candidate_id=candidate.candidate_id,
                arm=candidate.arm,
                validation=candidate.validation,
                source_question_ids=candidate.source_question_ids,
                source_estimated_tokens=transcript_tokens,
            )
        kwargs["compressed_candidates"] = corrected
    return plan_offline(source, **kwargs)


def _review(question_id: str, arm: QMSumArm, *, unsupported: bool = False):
    return QMSumReviewRecord(
        question_id=question_id,
        arm=arm,
        reviewer_kind=QMSumReviewKind.AGENT,
        reviewer_id="test-reviewer",
        protective_facts_complete=QMSumReviewVerdict.PASS,
        source_legality=QMSumReviewVerdict.PASS,
        roles_time_state_correct=QMSumReviewVerdict.PASS,
        reference_semantic_coverage=QMSumReviewVerdict.PASS,
        unsupported_claims=(QMSumReviewVerdict.FAIL if unsupported else QMSumReviewVerdict.PASS),
    )


def test_online_projection_has_no_reference_field_and_preserves_original_turns():
    source = _source()
    frozen = source.questions[0]
    projection = online_projection(frozen)
    payload = projection.payload()

    assert [turn["speaker"] for turn in payload["transcript"]] == ["Chair", "Member"]
    assert payload["transcript"] == [
        {
            "speaker": "Chair",
            "content": "We must keep the 20 minute limit.",
        },
        {
            "speaker": "Member",
            "content": "Proposal: schedule a follow-up. It is not approved or executed.",
        },
    ]
    assert payload["source_kind"] == "MEETING_TRANSCRIPT"
    assert payload["execution_record"] is False
    assert payload["turn_order"] == "NATURAL_SOURCE_ARRAY_ORDER"
    assert payload["trust"] == "UNTRUSTED_MEETING_DATA"
    assert "reference" not in str(payload)
    assert "answer" not in payload
    assert source.offline_reference(frozen.question_id).answer == "Synthetic reference answer"


def test_transcript_metadata_is_document_level_and_turn_order_is_array_order():
    projection = online_projection(_source().questions[0])
    payload = projection.payload()

    assert payload["trust"] == "UNTRUSTED_MEETING_DATA"
    assert payload["source_kind"] == "MEETING_TRANSCRIPT"
    assert payload["execution_record"] is False
    assert payload["turn_order"] == "NATURAL_SOURCE_ARRAY_ORDER"
    assert [set(turn) for turn in payload["transcript"]] == [
        {"speaker", "content"},
        {"speaker", "content"},
    ]
    assert [turn["content"] for turn in payload["transcript"]] == [
        turn.content for turn in projection.transcript
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"question": "q", "answer": "gold"},
        {"question": "q", "transcript": [{"relevant_text_span": [[0, 1]]}]},
        {"question": "q", "nested": {"reference_answer": "gold"}},
    ],
)
def test_online_guard_rejects_gold_fields_anywhere(payload):
    with pytest.raises(QMSumProtocolError, match="forbidden gold"):
        assert_safe_online_payload(payload)


def test_online_messages_use_task_and_untrusted_evidence_without_fake_execution():
    projection = online_projection(_source().questions[0])
    messages = build_online_messages(projection, arm=QMSumArm.B)

    assert [message.role for message in messages] == ["system", "user", "user"]
    assert [message.category for message in messages] == [
        ContextCategory.SYSTEM_PROMPT,
        ContextCategory.CONVERSATION,
        ContextCategory.CURRENT_USER_TASK,
    ]
    assert all(message.role != "assistant" for message in messages)
    assert all(message.role != "tool" for message in messages)
    assert '"trust":"UNTRUSTED_MEETING_DATA"' in (messages[1].message.content or "")
    assert '"execution_record":false' in (messages[1].message.content or "")
    assert '"turn_order":"NATURAL_SOURCE_ARRAY_ORDER"' in (messages[1].message.content or "")
    assert messages[1].message.content is not None
    evidence = json.loads(messages[1].message.content)
    assert evidence["transcript"][0] == ["Chair", "We must keep the 20 minute limit."]
    assert messages[2].message.content == projection.question
    assert "not user, assistant, or tool execution records" in messages[0].message.content
    assert "Synthetic reference answer" not in (messages[1].message.content or "")


def test_online_evidence_does_not_repeat_document_metadata_per_turn():
    projection = online_projection(_source().questions[0])
    messages = build_online_messages(projection, arm=QMSumArm.B)
    assert messages[1].message.content is not None
    evidence = json.loads(messages[1].message.content)
    serialized_turns = json.dumps(
        evidence["transcript"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert all(isinstance(turn, list) and len(turn) == 2 for turn in evidence["transcript"])
    assert '"trust"' not in serialized_turns
    assert '"source_kind"' not in serialized_turns
    assert '"execution_record"' not in serialized_turns


def test_committee_cases_are_counted_but_never_enter_online_execution():
    source = _source()
    policy = FakePolicy()
    plan = _plan(source, admission=policy)
    committee = [case for case in plan.cases if "covid_" in case.question_id]

    assert len(plan.cases) == 12
    assert len(committee) == 4
    assert all(case.status is QMSumCaseStatus.EXCLUDED for case in committee)
    assert all(
        case.blocked_reason == "COMMITTEE_EXCLUDED_FROM_ONLINE_EXPERIMENT" for case in committee
    )
    assert max(len(rows) for rows in policy.calls) > 0
    assert sum("covid_" in case.question_id for case in plan.cases) == 4
    with pytest.raises(QMSumProtocolError, match="forbidden"):
        validate_online_experiment_cases(
            source,
            [online_projection(source.questions[8])],
        )


def test_balanced_order_is_reproducible_and_evenly_assigns_first_arm():
    ids = [question.question_id for question in _source().questions]
    first = balanced_arm_orders(ids)
    second = balanced_arm_orders(ids)

    assert first == second
    assert sum(order.first is QMSumArm.B for order in first) == 6
    assert sum(order.first is QMSumArm.C for order in first) == 6
    assert {order.question_id for order in first} == set(ids)


def test_balanced_order_uses_a_permutation_and_covers_each_question_once():
    ids = [question.question_id for question in _source().questions]
    first = balanced_arm_orders(ids)
    other = balanced_arm_orders(ids, seed=7)

    assert len(first) == QMSUM_QUESTION_COUNT
    assert {order.question_id for order in first} == set(ids)
    assert first != other
    assert sum(order.first is QMSumArm.B for order in first) == 6
    assert sum(order.first is QMSumArm.C for order in first) == 6


def test_balanced_order_covers_every_frozen_question_in_global_sequence():
    source = _source()
    plan = _plan(source)

    assert {order.question_id for order in plan.orders} == {
        question.question_id for question in source.questions
    }
    assert sum(order.first is QMSumArm.B for order in plan.orders) == 6
    assert sum(order.first is QMSumArm.C for order in plan.orders) == 6
    first_arms = [case.arms[0].arm for case in plan.cases if len(case.arms) == 2]
    assert first_arms == [
        next(order.first for order in plan.orders if order.question_id == case.question_id)
        for case in plan.cases
        if len(case.arms) == 2
    ]


def test_online_case_validator_rejects_question_transcript_or_order_changes():
    source = _source()
    expected = tuple(
        online_projection(question)
        for question in source.questions
        if question.domain != "Committee"
    )
    assert validate_online_experiment_cases(source, expected) == expected

    first = expected[0]
    tampered_question = QMSumOnlineProjection(
        question_id=first.question_id,
        question=f"{first.question} changed",
        transcript=first.transcript,
    )
    with pytest.raises(QMSumProtocolError, match="differ"):
        validate_online_experiment_cases(
            source,
            (tampered_question, *expected[1:]),
        )

    tampered_transcript = QMSumOnlineProjection(
        question_id=first.question_id,
        question=first.question,
        transcript=(
            QMSumTranscriptTurn(0, first.transcript[0].speaker, "changed"),
            *first.transcript[1:],
        ),
    )
    with pytest.raises(QMSumProtocolError, match="differ"):
        validate_online_experiment_cases(
            source,
            (tampered_transcript, *expected[1:]),
        )

    with pytest.raises(QMSumProtocolError, match="differ"):
        validate_online_experiment_cases(source, tuple(reversed(expected)))


def test_plan_requires_b_admission_before_c_and_honors_order():
    source = _source()
    policy = FakePolicy()
    candidate_qid = source.questions[0].question_id
    plan = _plan(
        source,
        admission=policy,
        compressed_candidates={candidate_qid: _validated_candidate(candidate_qid)},
    )
    case = next(case for case in plan.cases if case.question_id == candidate_qid)

    assert case.status is QMSumCaseStatus.B_READY_C_READY
    assert {arm.arm for arm in case.arms} == {QMSumArm.B, QMSumArm.C}
    assert [arm.order for arm in case.arms] == [1, 2]
    assert len(policy.calls) >= 3
    assert case.arms[1].admitted.usage.estimated_input_after > 0


def test_real_context_policy_admits_complete_b_and_smaller_c():
    source = _source()
    qid = source.questions[0].question_id
    policy = _real_policy()
    candidate = _validated_candidate(qid, final_view='{"status":"OK"}')
    plan = _plan(
        source,
        admission=policy,
        compressed_candidates={qid: candidate},
    )
    case = next(case for case in plan.cases if case.question_id == qid)

    assert case.status is QMSumCaseStatus.B_READY_C_READY
    b = next(arm.admitted for arm in case.arms if arm.arm is QMSumArm.B)
    c = next(arm.admitted for arm in case.arms if arm.arm is QMSumArm.C)
    assert b.usage.truncated is False
    assert c.usage.truncated is False
    assert c.usage.estimated_input_after < b.usage.estimated_input_after
    assert b.message_categories == (
        ContextCategory.SYSTEM_PROMPT,
        ContextCategory.CONVERSATION,
        ContextCategory.CURRENT_USER_TASK,
    )


def test_real_context_policy_blocks_partial_b_and_prevents_c_admission():
    source = _source()
    qid = source.questions[0].question_id
    policy = _real_policy(context=512, retrieval=20)
    plan = _plan(
        source,
        admission=policy,
        compressed_candidates={qid: _validated_candidate(qid)},
    )
    case = next(case for case in plan.cases if case.question_id == qid)

    assert case.status is QMSumCaseStatus.B_BLOCKED
    assert case.arms == ()
    assert case.blocked_reason == "B_BUDGET_REJECTED:AGENT_CONTEXT_BUDGET_EXCEEDED"


@pytest.mark.parametrize("estimated,blocked", [(8414, False), (8415, True)])
def test_b_history_hard_threshold_is_enforced_before_dispatch(estimated, blocked):
    class BoundaryPolicy(FakePolicy):
        def admit(self, *args, **kwargs):
            result = super().admit(*args, **kwargs)
            return replace(
                result,
                usage=replace(
                    result.usage,
                    estimated_input_before=estimated,
                    estimated_input_after=estimated,
                ),
            )

    messages = build_online_messages(online_projection(_source().questions[0]), arm=QMSumArm.B)
    if blocked:
        with pytest.raises(QMSumProtocolError, match="hard threshold"):
            require_b_admission(BoundaryPolicy(), messages)
    else:
        admitted = require_b_admission(BoundaryPolicy(), messages)
        assert admitted.usage.estimated_input_after == estimated


def test_valid_summary_that_does_not_reduce_admitted_tokens_is_blocked():
    source = _source()
    qid = source.questions[0].question_id
    plan = _plan(
        source,
        admission=_real_policy(),
        compressed_candidates={
            qid: _validated_candidate(
                qid,
                final_view=json.dumps({"padded": "x" * 400}),
                claimed_source_tokens=4_000,
                validation_tokens=100,
            )
        },
    )
    case = next(case for case in plan.cases if case.question_id == qid)

    assert case.status is QMSumCaseStatus.C_BLOCKED
    assert [arm.arm for arm in case.arms] == [QMSumArm.B]
    assert "final view" in (case.blocked_reason or "")
    assert case.blocked_reason is not None


def test_b_budget_block_prevents_c_and_preserves_case_denominator():
    source = _source()
    rejected = source.questions[0].question_id
    policy = FakePolicy(fail_on=source.questions[0].transcript[0].content)
    plan = _plan(
        source,
        admission=policy,
        compressed_candidates={rejected: _validated_candidate(rejected)},
    )
    case = next(case for case in plan.cases if case.question_id == rejected)
    assert case.status is QMSumCaseStatus.B_BLOCKED
    assert case.arms == ()
    assert case.blocked_reason == "B_BUDGET_REJECTED:TEST_BUDGET_REJECTED"
    assert sum(case.question_id == rejected for case in plan.cases) == 1
    assert policy.calls[0] == (
        ContextCategory.SYSTEM_PROMPT,
        ContextCategory.CONVERSATION,
        ContextCategory.CURRENT_USER_TASK,
    )


def test_missing_summary_is_not_a_fabricated_c_call():
    source = _source()
    plan = _plan(source)
    academic_product = [case for case in plan.cases if "covid_" not in case.question_id]

    assert all(case.status is QMSumCaseStatus.B_READY_C_NOT_APPLICABLE for case in academic_product)
    assert all([arm.arm for arm in case.arms] == [QMSumArm.B] for case in academic_product)
    assert all(case.blocked_reason is None for case in academic_product)


def test_record_observations_obey_plan_order_and_reject_duplicate_extra_arms():
    source = _source()
    qid = source.questions[0].question_id
    plan = _plan(
        source,
        admission=FakePolicy(),
        compressed_candidates={qid: _validated_candidate(qid)},
    )
    planned_case = next(case for case in plan.cases if case.question_id == qid)
    candidate_id = next(
        arm.compressed_candidate_id for arm in planned_case.arms if arm.arm is QMSumArm.C
    )
    ordered = [arm.arm for arm in planned_case.arms]
    records = [
        QMSumArmRecord(
            qid,
            arm,
            f"output-{arm}",
            _measurements(100 if arm is QMSumArm.B else 40),
            summary_candidate_id=candidate_id if arm is QMSumArm.C else None,
        )
        for arm in ordered
    ]

    assert [row.arm for row in record_observations(plan, records)] == ordered
    with pytest.raises(QMSumProtocolError, match="order"):
        record_observations(plan, list(reversed(records)))


def test_record_observations_return_global_plan_order():
    source = _source()
    plan = _plan(source)
    records = []
    for case in plan.cases:
        for arm in case.arms:
            records.append(
                QMSumArmRecord(
                    case.question_id,
                    arm.arm,
                    f"output-{case.question_id}-{arm.arm}",
                    _measurements(100),
                )
            )

    shuffled = tuple(reversed(records))
    with pytest.raises(QMSumProtocolError, match="order"):
        record_observations(plan, shuffled)

    expected = [(case.question_id, arm.arm) for case in plan.cases for arm in case.arms]
    actual = [(record.question_id, record.arm) for record in record_observations(plan, records)]
    assert actual == expected


def test_score_summary_keeps_review_denominators_and_no_auto_semantic_metric():
    source = _source()
    qid = source.questions[0].question_id
    plan = _plan(
        source,
        admission=FakePolicy(),
        compressed_candidates={qid: _validated_candidate(qid)},
    )
    b = QMSumArmRecord(
        qid,
        QMSumArm.B,
        "B answer",
        _measurements(100),
        review=_review(qid, QMSumArm.B),
    )
    c = QMSumArmRecord(
        qid,
        QMSumArm.C,
        "C answer",
        _measurements(70),
        review=_review(qid, QMSumArm.C, unsupported=True),
        summary_candidate_id=(
            next(case for case in plan.cases if case.question_id == qid)
            .arms[
                next(
                    index
                    for index, arm in enumerate(
                        next(case for case in plan.cases if case.question_id == qid).arms
                    )
                    if arm.arm is QMSumArm.C
                )
            ]
            .compressed_candidate_id
        ),
    )
    summary = summarize_score_records(plan, (b, c))

    assert summary.frozen_questions == 12
    assert summary.online_eligible_questions == 8
    assert summary.committee_excluded_questions == 4
    assert summary.budget_blocked_questions == 0
    assert summary.planned_b_dispatchable_questions == 8
    assert summary.planned_c_dispatchable_questions == 1
    assert summary.observed_questions == 1
    assert summary.observed_arms == 2
    assert summary.answerable_pairs == 1
    assert summary.review_count == 2
    assert summary.agent_review_count == 2
    assert summary.human_review_count == 0
    assert summary.arm_qualified_counts == {QMSumArm.B: 1, QMSumArm.C: 0}
    assert summary.paired_task_qualified_counts == 0
    assert summary.unsupported_claim_counts == {QMSumArm.B: 0, QMSumArm.C: 1}
    assert summary.paired_input_token_median_reduction == pytest.approx(0.30)
    assert summary.semantic_accuracy == "NOT_AUTOMATICALLY_SCORED"


def test_summary_retains_failed_unknown_and_unreviewed_denominators():
    source = _source()
    qid = source.questions[0].question_id
    plan = _plan(source, compressed_candidates={qid: _validated_candidate(qid)})
    case = next(case for case in plan.cases if case.question_id == qid)
    first, second = case.arms
    records = (
        QMSumArmRecord(
            qid,
            first.arm,
            "",
            _measurements(100),
            summary_candidate_id=first.compressed_candidate_id,
            status=QMSumObservationStatus.FAILED,
        ),
        QMSumArmRecord(
            qid,
            second.arm,
            "",
            _measurements(None, None),
            summary_candidate_id=second.compressed_candidate_id,
            status=QMSumObservationStatus.UNKNOWN_OUTCOME,
        ),
    )
    report = summarize_score_records(plan, records)
    assert report.frozen_questions == 12 and report.committee_excluded_questions == 4
    assert report.attempted_arms == 2 and report.observed_arms == 0
    assert report.failed_arms == 1 and report.unknown_outcome_arms == 1
    assert report.failed_pairs == 1 and report.unknown_pairs == 1
    assert report.answerable_pairs == 0 and report.review_count == 0
    assert report.token_reduction_under_pressure is None and report.stress_proof == "UNVERIFIED"


def test_unreviewed_output_and_negative_token_gain_are_not_hidden():
    source = _source()
    qid = source.questions[0].question_id
    plan = _plan(source, compressed_candidates={qid: _validated_candidate(qid)})
    case = next(case for case in plan.cases if case.question_id == qid)
    records = tuple(
        QMSumArmRecord(
            qid,
            arm.arm,
            "observed",
            _measurements(100 if arm.arm is QMSumArm.B else 150),
            summary_candidate_id=arm.compressed_candidate_id,
        )
        for arm in case.arms
    )
    report = summarize_score_records(plan, records)
    assert report.observed_arms == 2 and report.missing_reviews == 2
    assert report.answerable_pairs == 0
    assert report.paired_input_token_median_reduction == pytest.approx(-0.5)
    assert report.token_reduction_under_pressure is None


def test_pressure_gain_uses_actual_baseline_usage_and_a_qualified_answer():
    source = _source()
    qid = source.questions[0].question_id
    plan = _plan(source, compressed_candidates={qid: _validated_candidate(qid)})
    case = next(case for case in plan.cases if case.question_id == qid)
    records = tuple(
        QMSumArmRecord(
            qid,
            arm.arm,
            "observed",
            _measurements(7000 if arm.arm is QMSumArm.B else 3500),
            review=_review(qid, arm.arm),
            summary_candidate_id=arm.compressed_candidate_id,
        )
        for arm in case.arms
    )
    report = summarize_score_records(plan, records)
    assert report.pressure_valid_pairs == 1
    assert report.token_reduction_under_pressure == pytest.approx(0.5)
    assert report.stress_proof == "UNVERIFIED"


def test_actual_usage_at_hard_threshold_is_not_claimed_as_a_valid_pressure_pair():
    source = _source()
    qid = source.questions[0].question_id
    plan = _plan(source, compressed_candidates={qid: _validated_candidate(qid)})
    case = next(case for case in plan.cases if case.question_id == qid)
    records = tuple(
        QMSumArmRecord(
            qid,
            arm.arm,
            "observed",
            _measurements(8415 if arm.arm is QMSumArm.B else 3500),
            review=_review(qid, arm.arm),
            summary_candidate_id=arm.compressed_candidate_id,
        )
        for arm in case.arms
    )
    report = summarize_score_records(plan, records)
    assert report.pressure_valid_pairs == 0
    assert report.token_reduction_under_pressure is None


def test_lexical_score_requires_explicit_reference_and_is_not_semantic_accuracy():
    source = _source()
    question = source.questions[0]
    score = lexical_reference_scores(
        source.offline_reference(question.question_id),
        question_id=question.question_id,
        arm=QMSumArm.B,
        prediction="Synthetic reference answer",
    )

    assert score.token_f1 == 1.0
    assert score.reference_count == 1


def test_lexical_f1_counts_overlapping_words_not_equal_lengths():
    from benchmarks.evaluation.memory_qmsum_protocol import sentence_token_f1

    assert sentence_token_f1("red blue", "green yellow") == 0.0
    assert sentence_token_f1("red red blue", "red green green") == pytest.approx(1 / 3)


def test_synthetic_source_cannot_enter_execution_plan():
    source = _source()
    synthetic = QMSumSourceBundle(
        source.commit,
        source.meetings,
        source.questions,
        provenance=QMSumSourceProvenance.SYNTHETIC_FIXTURE,
        _references=source.offline_references(),
    )

    with pytest.raises(QMSumProtocolError, match="synthetic"):
        plan_offline(
            synthetic,
            admission=FakePolicy(),
            token_counter=CharacterEstimator(),
        )


def test_offline_manifest_discloses_committee_and_stress_limits():
    from benchmarks.evaluation.memory_qmsum_protocol import protocol_manifest

    manifest = protocol_manifest(_source())
    assert manifest["meeting_count"] == 6
    assert manifest["question_count"] == 12
    assert manifest["source_commit"] == "83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e"
    assert manifest["committee_online_policy"] == "EXCLUDED_PENDING_PERMISSION_REVIEW"
    assert manifest["stress_proof"].startswith("UNVERIFIED")
