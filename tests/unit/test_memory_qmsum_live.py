"""Formal trial registration and restart must not issue duplicate calls."""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from benchmarks.evaluation.memory_qmsum_live import (
    freeze_summary_inputs,
    prepare_reviewed_summaries,
    run_live_summaries,
    verify_resumed_result,
)
from benchmarks.evaluation.memory_qmsum_protocol import QMSumCaseStatus, QMSumProtocolError
from benchmarks.evaluation.memory_trial_budget import MemoryTrialBudget
from benchmarks.evaluation.memory_trial_client import (
    MODEL,
    PRICE_IDENTITY,
    trial_request_identity,
    trial_result_identity,
)
from packages.model_gateway.contracts import ModelMessage
from tests.unit.test_memory_qmsum_summary import _prepared_case


@pytest.fixture
def trial():
    root = Path(".scratch/context-policy-m4-20261027/live-tests") / uuid4().hex
    root.mkdir(parents=True)
    source, plan, question, policy = _prepared_case()
    changed = tuple(
        case
        if case.question_id == question.question_id or case.status is QMSumCaseStatus.EXCLUDED
        else replace(case, status=QMSumCaseStatus.B_BLOCKED, arms=(), blocked_reason="test")
        for case in plan.cases
    )
    plan = replace(plan, cases=changed)
    annotations = {
        "format": "qmsum-reviewed-turns-v1",
        "cases": [{"question_id": question.question_id, "turns": [0]}],
    }
    preparations = prepare_reviewed_summaries(source, plan, annotations, policy=policy)
    return root, source, plan, question, policy, annotations, preparations


def test_annotation_and_exact_input_are_frozen_before_any_dispatch(trial):
    root, source, plan, question, policy, annotations, preparations = trial
    preparation = preparations[0]
    assert preparation.context.protected_facts[0].text.startswith("[0] Chair:")
    path = root / "frozen.json"
    freeze_summary_inputs(preparations, path)
    original = path.read_bytes()
    freeze_summary_inputs(preparations, path)
    changed = replace(
        preparation,
        messages=(ModelMessage("system", "changed"), preparation.messages[1]),
    )
    with pytest.raises(QMSumProtocolError, match="changed after registration"):
        freeze_summary_inputs((changed,), path)
    assert path.read_bytes() == original

    bad = {
        "format": annotations["format"],
        "cases": [{"question_id": question.question_id, "turns": [0, 0]}],
    }
    with pytest.raises(QMSumProtocolError, match="unique and ordered"):
        prepare_reviewed_summaries(source, plan, bad, policy=policy)


def _response(preparation, attempt_id):
    source_id = preparation.context.sources[0].source_id
    content = json.dumps(
        {
            "status": "OK",
            "goal": {"text": "continue", "source_ids": [source_id]},
            "decisions": [],
            "supported_facts": [],
            "conflicts": [],
            "open_questions": [],
            "completed_actions_refs": [],
            "next_steps": [],
            "omitted_source_ids": [],
        }
    )
    return {
        "attempt_id": attempt_id,
        "stage": "M4",
        "model": MODEL,
        "content": content,
        "finish_reason": "stop",
        "input_tokens": 21000,
        "output_tokens": 500,
        "conservative_cost_cny": "0.046",
        "cost_identity": PRICE_IDENTITY,
    }


def test_confirmed_result_is_reconstructed_without_a_second_call(trial):
    root, _, _, _, _, _, preparations = trial
    attempt = "m4-summary-Bmr003-0"
    ledger = root / "ledger.json"
    frozen = root / "frozen.json"
    endpoint = "https://demo.cn-beijing.maas.aliyuncs.com/v1"
    freeze_summary_inputs(preparations, frozen)
    request_identity = trial_request_identity(
        endpoint=endpoint, messages=preparations[0].messages, output_limit=2048
    )
    response = {**_response(preparations[0], attempt), "request_identity": request_identity}
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        budget.reserve(
            attempt,
            stage="M4",
            estimated_input_tokens=preparations[0].admitted.usage.estimated_input_after,
            requested_output_tokens=2048,
            upper_cost_cny=Decimal("0.1"),
            request_identity=request_identity,
        )
        budget.settle(
            attempt,
            conservative_cost_cny=Decimal("0.046"),
            result_identity=trial_result_identity(response),
        )
    results = root / "results"
    results.mkdir()
    (results / f"{attempt}.json").write_text(
        json.dumps(response), encoding="utf-8"
    )
    result = asyncio.run(
        run_live_summaries(
            preparations,
            ledger_path=ledger,
            result_directory=results,
            frozen_input_path=frozen,
            endpoint=endpoint,
            api_key="unused",
        )
    )
    assert [item.status for item in result.observations] == ["VALID"]
    assert not result.unattempted_question_ids


def test_unknown_previous_attempt_stays_unknown_without_retry(trial):
    root, _, _, _, _, _, preparations = trial
    ledger = root / "ledger.json"
    freeze_summary_inputs(preparations, root / "frozen.json")
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        budget.reserve(
            "m4-summary-Bmr003-0",
            stage="M4",
            estimated_input_tokens=preparations[0].admitted.usage.estimated_input_after,
            requested_output_tokens=2048,
            upper_cost_cny=Decimal("0.1"),
        )
    result = asyncio.run(
        run_live_summaries(
            preparations,
            ledger_path=ledger,
            result_directory=root / "results",
            frozen_input_path=root / "frozen.json",
            endpoint="invalid-if-used",
            api_key="unused",
        )
    )
    assert [item.status for item in result.observations] == ["UNKNOWN_OUTCOME"]


def test_counter_evidence_mismatch_on_restart_never_dispatches(trial):
    root, _, _, _, _, _, preparations = trial
    attempt = "m4-summary-Bmr003-0"
    ledger = root / "ledger.json"
    endpoint = "https://demo.cn-beijing.maas.aliyuncs.com/v1"
    freeze_summary_inputs(preparations, root / "frozen.json")
    request_identity = trial_request_identity(
        endpoint=endpoint, messages=preparations[0].messages, output_limit=2048
    )
    response = {**_response(preparations[0], attempt), "request_identity": request_identity}
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        budget.reserve(
            attempt,
            stage="M4",
            estimated_input_tokens=32768,
            requested_output_tokens=2048,
            upper_cost_cny=Decimal("0.1"),
            request_identity=request_identity,
            input_counter={
                "counter_id": "deepseek_v41_text_v1",
                "tokenizer_revision": "9f9818127fdfbb60feba204799d617a874b32a86",
                "thinking_mode": "PROVIDER_DEFAULT_THINKING",
                "tokens": 100,
                "input_limit": preparations[0].admitted.usage.available_input,
            },
        )
        budget.settle(
            attempt,
            conservative_cost_cny=Decimal("0.046"),
            result_identity=trial_result_identity(response),
        )
    results = root / "results"
    results.mkdir()
    (results / f"{attempt}.json").write_text(
        json.dumps(response), encoding="utf-8"
    )
    result = asyncio.run(
        run_live_summaries(
            preparations,
            ledger_path=ledger,
            result_directory=results,
            frozen_input_path=root / "frozen.json",
            endpoint=endpoint,
            api_key="unused",
        )
    )
    assert [item.status for item in result.observations] == ["UNVERIFIED_RESPONSE"]
    assert result.observations[0].issues == ("RESUME_IDENTITY_UNVERIFIED",)


def test_invalid_endpoint_is_known_not_dispatched(trial):
    root, _, _, _, _, _, preparations = trial
    result = asyncio.run(
        run_live_summaries(
            preparations,
            ledger_path=root / "ledger.json",
            result_directory=root / "results",
            frozen_input_path=root / "frozen.json",
            endpoint="invalid-if-used",
            api_key="unused",
        )
    )
    assert [item.status for item in result.observations] == ["NOT_DISPATCHED"]
    with MemoryTrialBudget(root / "ledger.json", price_identity=PRICE_IDENTITY) as budget:
        assert budget.state["attempts"] == []


def test_existing_trial_never_recreates_a_missing_summary_freeze(trial):
    root, _, _, _, _, _, preparations = trial
    ledger = root / "ledger.json"
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        budget.reserve(
            "m4-summary-Bmr003-0",
            stage="M4",
            estimated_input_tokens=32768,
            requested_output_tokens=2048,
            upper_cost_cny=Decimal("0.1"),
        )
    frozen = root / "missing.json"
    with pytest.raises(QMSumProtocolError, match="frozen summary input is missing"):
        asyncio.run(
            run_live_summaries(
                preparations,
                ledger_path=ledger,
                result_directory=root / "results",
                frozen_input_path=frozen,
                endpoint="invalid-if-used",
                api_key="unused",
            )
        )
    assert not frozen.exists()


def test_legacy_result_without_request_binding_is_not_reused(trial):
    root, _, _, _, _, _, preparations = trial
    attempt = "m4-summary-Bmr003-0"
    ledger = root / "ledger.json"
    freeze_summary_inputs(preparations, root / "frozen.json")
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        budget.reserve(
            attempt,
            stage="M4",
            estimated_input_tokens=32768,
            requested_output_tokens=2048,
            upper_cost_cny=Decimal("0.1"),
        )
        budget.settle(attempt, conservative_cost_cny=Decimal("0.046"))
    results = root / "results"
    results.mkdir()
    (results / f"{attempt}.json").write_text(
        json.dumps(_response(preparations[0], attempt)), encoding="utf-8"
    )
    recovered = asyncio.run(
        run_live_summaries(
            preparations,
            ledger_path=ledger,
            result_directory=results,
            frozen_input_path=root / "frozen.json",
            endpoint="invalid-if-used",
            api_key="unused",
        )
    )
    assert recovered.observations[0].status == "UNVERIFIED_RESPONSE"
    assert recovered.observations[0].issues == ("RESUME_IDENTITY_UNVERIFIED",)
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        assert len(budget.state["attempts"]) == 1


def test_settled_attempt_with_missing_result_is_unverified_not_unknown(trial):
    root, _, _, _, _, _, preparations = trial
    attempt = "m4-summary-Bmr003-0"
    ledger = root / "ledger.json"
    freeze_summary_inputs(preparations, root / "frozen.json")
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        budget.reserve(
            attempt,
            stage="M4",
            estimated_input_tokens=32768,
            requested_output_tokens=2048,
            upper_cost_cny=Decimal("0.1"),
        )
        budget.settle(attempt, conservative_cost_cny=Decimal("0.046"))
    recovered = asyncio.run(
        run_live_summaries(
            preparations,
            ledger_path=ledger,
            result_directory=root / "results",
            frozen_input_path=root / "frozen.json",
            endpoint="invalid-if-used",
            api_key="unused",
        )
    )
    assert recovered.observations[0].status == "UNVERIFIED_RESPONSE"
    assert recovered.observations[0].issues == ("RESUME_IDENTITY_UNVERIFIED",)


def test_resume_rejects_changed_request_or_saved_result(trial):
    root, _, _, _, _, _, preparations = trial
    preparation = preparations[0]
    attempt = "m4-summary-Bmr003-0"
    endpoint = "https://demo.cn-beijing.maas.aliyuncs.com/v1"
    original = trial_request_identity(
        endpoint=endpoint, messages=preparation.messages, output_limit=2048
    )
    result = {**_response(preparation, attempt), "request_identity": original}
    ledger = root / "ledger.json"
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        budget.reserve(
            attempt,
            stage="M4",
            estimated_input_tokens=32768,
            requested_output_tokens=2048,
            upper_cost_cny=Decimal("0.1"),
            request_identity=original,
        )
        budget.settle(
            attempt,
            conservative_cost_cny=Decimal("0.046"),
            result_identity=trial_result_identity(result),
        )
        row = dict(budget.state["attempts"][0])
    changed_messages = (ModelMessage("system", "different"), preparation.messages[1])
    changed_request = trial_request_identity(
        endpoint=endpoint, messages=changed_messages, output_limit=2048
    )
    with pytest.raises(ValueError, match="RESULT_MISMATCH"):
        verify_resumed_result(
            result, row, preparation.admitted, attempt, output_limit=2048,
            expected_request_identity=changed_request,
        )
    with pytest.raises(ValueError, match="RESULT_MISMATCH"):
        verify_resumed_result(
            {**result, "content": "modified after settlement"},
            row, preparation.admitted, attempt, output_limit=2048,
            expected_request_identity=original,
        )
    assert preparation.context.sources[0].content not in ledger.read_text(encoding="utf-8")
