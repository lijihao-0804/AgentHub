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
)
from benchmarks.evaluation.memory_qmsum_protocol import QMSumCaseStatus, QMSumProtocolError
from benchmarks.evaluation.memory_trial_budget import MemoryTrialBudget
from benchmarks.evaluation.memory_trial_client import MODEL, PRICE_IDENTITY
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
    with MemoryTrialBudget(ledger, price_identity=PRICE_IDENTITY) as budget:
        budget.reserve(
            attempt,
            stage="M4",
            estimated_input_tokens=preparations[0].admitted.usage.estimated_input_after,
            requested_output_tokens=2048,
            upper_cost_cny=Decimal("0.1"),
        )
        budget.settle(attempt, conservative_cost_cny=Decimal("0.046"))
    results = root / "results"
    results.mkdir()
    (results / f"{attempt}.json").write_text(
        json.dumps(_response(preparations[0], attempt)), encoding="utf-8"
    )
    result = asyncio.run(
        run_live_summaries(
            preparations,
            ledger_path=ledger,
            result_directory=results,
            frozen_input_path=root / "frozen.json",
            endpoint="invalid-if-used",
            api_key="unused",
        )
    )
    assert [item.status for item in result.observations] == ["VALID"]
    assert not result.unattempted_question_ids


def test_unknown_previous_attempt_stays_unknown_without_retry(trial):
    root, _, _, _, _, _, preparations = trial
    ledger = root / "ledger.json"
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
