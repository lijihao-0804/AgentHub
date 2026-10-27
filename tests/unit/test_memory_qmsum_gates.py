"""Focused QMSum gates for invalid summaries and terminal pair outcomes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from benchmarks.evaluation.memory_qmsum_answers import (
    execute_answer_plan,
    plan_answer_phase,
    run_live_answers,
)
from benchmarks.evaluation.memory_qmsum_protocol import (
    QMSumArm,
    QMSumCaseStatus,
    QMSumProtocolError,
    summarize_score_records,
)
from benchmarks.evaluation.memory_qmsum_summary_runner import (
    QMSumSummaryObservation,
    QMSumSummaryRun,
    SummaryNotDispatched,
    SummaryResumeUnverified,
)
from benchmarks.evaluation.memory_trial_budget import MemoryTrialBudget
from benchmarks.evaluation.memory_trial_client import MODEL, PRICE_IDENTITY
from tests.unit.test_memory_qmsum_protocol import (
    FakePolicy,
    _plan,
    _source,
    _validated_candidate,
)


def _invalid_summary_plan():
    source = _source()
    observations = tuple(
        QMSumSummaryObservation(
            question.question_id,
            "INVALID",
            {},
            ("PROVIDER_RESPONSE_INCOMPLETE",),
        )
        for question in source.questions
        if question.domain == "Product"
    )
    invalid_reasons = {
        observation.question_id: "PROVIDER_RESPONSE_INCOMPLETE" for observation in observations
    }
    plan = plan_answer_phase(
        source,
        QMSumSummaryRun(observations, ()),
        policy=FakePolicy(fail_on="Synthetic query B"),
    )
    return plan, invalid_reasons


def _answer_response(attempt_id: str) -> dict[str, object]:
    return {
        "attempt_id": attempt_id,
        "stage": "M4",
        "model": MODEL,
        "content": "answer",
        "finish_reason": "stop",
        "input_tokens": 100,
        "output_tokens": 10,
        "elapsed_ms": 1.0,
        "conservative_cost_cny": "0.00028",
        "cost_identity": PRICE_IDENTITY,
    }


def _one_pair_plan():
    source = _source()
    question_id = source.questions[0].question_id
    plan = _plan(
        source,
        compressed_candidates={question_id: _validated_candidate(question_id)},
    )
    cases = tuple(
        case
        if case.question_id == question_id
        else replace(
            case,
            status=QMSumCaseStatus.B_BLOCKED,
            arms=(),
            blocked_reason="ISOLATED_TEST",
        )
        for case in plan.cases
    )
    return replace(plan, cases=cases), question_id


@pytest.mark.asyncio
async def test_four_invalid_summaries_terminate_pairs_without_dispatch():
    plan, invalid_reasons = _invalid_summary_plan()
    invalid_cases = [
        case for case in plan.cases if case.status is QMSumCaseStatus.C_SUMMARY_INVALID
    ]
    assert {case.question_id for case in invalid_cases} == set(invalid_reasons)
    assert all(not case.arms for case in invalid_cases)
    assert all(case.blocked_reason == "PROVIDER_RESPONSE_INCOMPLETE" for case in invalid_cases)

    calls: list[tuple[str, QMSumArm]] = []

    async def dispatch(question_id, arm):
        calls.append((question_id, arm.arm))
        return _answer_response(f"unexpected-{len(calls)}")

    result = await execute_answer_plan(plan, dispatch=dispatch)

    assert calls == []
    assert result.records == () and result.unattempted == ()
    assert result.stop_reason is None
    assert len(result.terminal_cases) == 4
    assert {case.question_id for case in result.terminal_cases} == set(invalid_reasons)
    assert all(
        case.status is QMSumCaseStatus.C_SUMMARY_INVALID
        and case.reason == "PROVIDER_RESPONSE_INCOMPLETE"
        for case in result.terminal_cases
    )
    report = summarize_score_records(plan, result.records)
    assert report.invalid_summary_questions == 4
    assert report.planned_pairs == 0 and report.complete_observed_pairs == 0


def test_missing_summary_without_invalid_outcome_remains_c_not_applicable():
    source = _source()
    plan = plan_answer_phase(
        source,
        QMSumSummaryRun((), ()),
        policy=FakePolicy(),
    )
    product = next(question for question in source.questions if question.domain == "Product")
    case = next(item for item in plan.cases if item.question_id == product.question_id)

    assert case.status is QMSumCaseStatus.B_READY_C_NOT_APPLICABLE
    assert case.blocked_reason is None
    assert [arm.arm for arm in case.arms] == [QMSumArm.B]


def test_incomplete_summary_observations_cannot_be_treated_as_inapplicable():
    source = _source()
    expected = tuple(q.question_id for q in source.questions if q.domain == "Product")
    assert expected
    with pytest.raises(QMSumProtocolError, match="do not match the frozen preparations"):
        plan_answer_phase(
            source,
            QMSumSummaryRun((), ()),
            policy=FakePolicy(),
            expected_summary_ids=expected,
        )


def test_invalid_summary_gate_does_not_grow_ledger(tmp_path):
    plan, _ = _invalid_summary_plan()
    ledger = tmp_path / "ledger.json"
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        budget.reserve(
            "ledger-sentinel",
            stage="CALIBRATION",
            estimated_input_tokens=1,
            requested_output_tokens=1,
            upper_cost_cny=Decimal("0.01"),
        )
        budget.settle("ledger-sentinel", conservative_cost_cny=Decimal("0"))
    ledger_before = ledger.read_bytes()

    result = asyncio.run(
        run_live_answers(
            plan,
            ledger_path=ledger,
            result_directory=tmp_path / "responses",
            frozen_input_path=tmp_path / "frozen-answer-inputs.json",
            endpoint="invalid-if-used",
            api_key="unused",
        )
    )

    assert ledger.read_bytes() == ledger_before
    assert result.records == () and result.unattempted == ()
    assert len(result.terminal_cases) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["FAILED", "UNKNOWN_OUTCOME", "UNVERIFIED_RESPONSE"])
@pytest.mark.parametrize("halt_arm_index", [0, 1])
async def test_first_and_second_arm_terminal_outcomes_count_each_pair_once(
    terminal_status, halt_arm_index
):
    plan, _ = _one_pair_plan()
    calls: list[QMSumArm] = []

    async def dispatch(question_id, arm):
        calls.append(arm.arm)
        call_index = len(calls) - 1
        if call_index == halt_arm_index:
            if terminal_status == "UNKNOWN_OUTCOME":
                raise TimeoutError("provider outcome unknown")
            if terminal_status == "UNVERIFIED_RESPONSE":
                raise SummaryResumeUnverified
            return {
                **_answer_response(f"failed-{call_index}"),
                "input_counter": {"tokens": 99},
            }
        return _answer_response(f"observed-{call_index}")

    result = await execute_answer_plan(plan, dispatch=dispatch)
    report = summarize_score_records(plan, result.records)

    assert len(calls) == halt_arm_index + 1
    assert report.planned_pairs == 1
    assert report.failed_pairs == int(terminal_status == "FAILED")
    assert report.unknown_pairs == int(terminal_status == "UNKNOWN_OUTCOME")
    assert report.unverified_pairs == int(terminal_status == "UNVERIFIED_RESPONSE")
    assert report.partial_pairs == int(halt_arm_index == 0)
    assert report.partial_failed_pairs == int(halt_arm_index == 0 and terminal_status == "FAILED")
    assert report.partial_unknown_pairs == int(
        halt_arm_index == 0 and terminal_status == "UNKNOWN_OUTCOME"
    )
    assert report.partial_unverified_pairs == int(
        halt_arm_index == 0 and terminal_status == "UNVERIFIED_RESPONSE"
    )
    assert report.not_started_pairs == 0


@pytest.mark.asyncio
async def test_known_predispatch_stop_counts_pair_as_not_started():
    plan, _ = _one_pair_plan()

    async def dispatch(_question_id, _arm):
        raise SummaryNotDispatched

    result = await execute_answer_plan(plan, dispatch=dispatch)
    report = summarize_score_records(plan, result.records)

    assert result.records == ()
    assert report.planned_pairs == 1
    assert report.partial_pairs == 0
    assert report.not_started_pairs == 1
    assert report.failed_pairs == 0 and report.unknown_pairs == 0


@pytest.mark.asyncio
async def test_malformed_completed_response_is_unverified_not_unknown():
    plan, _ = _one_pair_plan()

    async def dispatch(_question_id, _arm):
        return {"content": None}

    result = await execute_answer_plan(plan, dispatch=dispatch)
    report = summarize_score_records(plan, result.records)
    assert result.stop_reason == "UNVERIFIED_RESPONSE"
    assert report.unverified_response_arms == 1
    assert report.unverified_pairs == 1 and report.partial_unverified_pairs == 1
    assert report.unknown_outcome_arms == 0 and report.unknown_pairs == 0
