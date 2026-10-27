"""The QMSum B/C coordinator preserves order, failures, and durable restarts."""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from benchmarks.evaluation.memory_qmsum_answers import (
    ANSWER_OUTPUT_LIMIT,
    execute_answer_plan,
    freeze_answer_inputs,
    run_live_answers,
)
from benchmarks.evaluation.memory_qmsum_protocol import QMSumCaseStatus, QMSumProtocolError
from benchmarks.evaluation.memory_qmsum_summary_runner import SummaryNotDispatched
from benchmarks.evaluation.memory_trial_budget import MemoryTrialBudget
from benchmarks.evaluation.memory_trial_client import MODEL, PRICE_IDENTITY
from packages.model_gateway.contracts import ModelMessage
from tests.unit.test_memory_qmsum_protocol import _plan, _source, _validated_candidate


def _response(attempt_id="test", *, finish_reason="stop"):
    return {
        "attempt_id": attempt_id,
        "stage": "M4",
        "model": MODEL,
        "content": "answer",
        "finish_reason": finish_reason,
        "input_tokens": 100,
        "output_tokens": 10,
        "elapsed_ms": 1.0,
        "conservative_cost_cny": "0.00028",
        "cost_identity": PRICE_IDENTITY,
    }


@pytest.mark.asyncio
async def test_order_and_denominator_survive_a_failed_then_unknown_answer():
    source = _source()
    qid = source.questions[0].question_id
    plan = _plan(source, compressed_candidates={qid: _validated_candidate(qid)})
    expected = [(case.question_id, arm.arm) for case in plan.cases for arm in case.arms]
    calls = []

    async def dispatch(question_id, arm):
        calls.append((question_id, arm.arm))
        if len(calls) == 3:
            raise TimeoutError("unknown provider outcome")
        return _response(
            f"test-{len(calls)}", finish_reason="length" if len(calls) == 2 else "stop"
        )

    result = await execute_answer_plan(plan, dispatch=dispatch)
    assert calls == expected[:3]
    assert [record.status.value for record in result.records] == [
        "OBSERVED",
        "FAILED",
        "UNKNOWN_OUTCOME",
    ]
    assert result.unattempted == tuple(expected[3:])
    assert result.stop_reason == "UNKNOWN_OUTCOME"
    assert all("content" not in metadata for metadata in result.measurements.values())


@pytest.mark.asyncio
async def test_known_predispatch_rejection_does_not_fabricate_an_attempt():
    plan = _plan(_source())

    async def dispatch(_question_id, _arm):
        raise SummaryNotDispatched

    result = await execute_answer_plan(plan, dispatch=dispatch)
    assert result.records == ()
    assert result.stop_reason == "NOT_DISPATCHED"
    assert result.unattempted == tuple(
        (case.question_id, arm.arm) for case in plan.cases for arm in case.arms
    )


@pytest.mark.asyncio
async def test_answer_with_changed_provider_token_count_is_not_scored_as_observed():
    source = _source()
    first_id = source.questions[0].question_id
    plan = _plan(source, compressed_candidates={first_id: _validated_candidate(first_id)})
    calls = []

    async def dispatch(question_id, arm):
        calls.append((question_id, arm.arm))
        if len(calls) > 1:
            raise SummaryNotDispatched
        return {**_response(), "input_counter": {"tokens": 99}}

    result = await execute_answer_plan(plan, dispatch=dispatch)
    assert result.records[0].status.value == "FAILED"
    assert result.stop_reason == "PROVIDER_COUNTER_MISMATCH"
    assert len(calls) == 1


def test_answer_freeze_rejects_any_later_message_change():
    root = Path(".scratch/context-policy-m4-20261027/answer-tests") / uuid4().hex
    root.mkdir(parents=True)
    plan = _plan(_source())
    path = root / "frozen.json"
    freeze_answer_inputs(plan, path)
    original = path.read_bytes()
    first = plan.cases[0]
    arm = first.arms[0]
    changed_admission = replace(
        arm.admitted,
        messages=(ModelMessage("system", "changed"), *arm.admitted.messages[1:]),
    )
    changed_case = replace(first, arms=(replace(arm, admitted=changed_admission),))
    changed_plan = replace(plan, cases=(changed_case, *plan.cases[1:]))
    with pytest.raises(QMSumProtocolError, match="changed after registration"):
        freeze_answer_inputs(changed_plan, path)
    assert path.read_bytes() == original


def test_confirmed_answer_is_reused_without_new_dispatch():
    root = Path(".scratch/context-policy-m4-20261027/answer-tests") / uuid4().hex
    root.mkdir(parents=True)
    plan = _plan(_source())
    first = plan.cases[0]
    cases = tuple(
        case
        if case == first or case.status is QMSumCaseStatus.EXCLUDED
        else replace(case, status=QMSumCaseStatus.B_BLOCKED, arms=(), blocked_reason="test")
        for case in plan.cases
    )
    plan = replace(plan, cases=cases)
    attempt = "m4-answer-Bmr003-0-B"
    ledger = root / "ledger.json"
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        budget.reserve(
            attempt,
            stage="M4",
            estimated_input_tokens=first.arms[0].admitted.usage.estimated_input_after,
            requested_output_tokens=ANSWER_OUTPUT_LIMIT,
            upper_cost_cny=Decimal("0.1"),
        )
        budget.settle(attempt, conservative_cost_cny=Decimal("0.00028"))
    results = root / "results"
    results.mkdir()
    (results / f"{attempt}.json").write_text(json.dumps(_response(attempt)), encoding="utf-8")
    result = asyncio.run(
        run_live_answers(
            plan,
            ledger_path=ledger,
            result_directory=results,
            frozen_input_path=root / "frozen.json",
            endpoint="invalid-if-used",
            api_key="unused",
        )
    )
    assert [record.status.value for record in result.records] == ["OBSERVED"]
    assert not result.unattempted and result.stop_reason is None
