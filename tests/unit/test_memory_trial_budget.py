"""Financial and attempt limits apply before the evaluation sends a request."""

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from benchmarks.evaluation.memory_trial_budget import MemoryTrialBudget, upper_bound_cost_cny


@pytest.fixture
def trial_dir():
    # pytest's restrictive temp-directory mode is not accessible in the
    # Windows sandbox; use a fresh project-local ignored directory instead.
    path = Path(".scratch/context-policy-m4-20261027/budget-tests") / uuid4().hex
    path.mkdir(parents=True)
    return path


def _reserve(budget, attempt="attempt-1", **overrides):
    params = {
        "stage": "M4",
        "estimated_input_tokens": 100,
        "requested_output_tokens": 50,
        "upper_cost_cny": Decimal("1"),
    }
    params.update(overrides)
    budget.reserve(attempt, **params)


def test_ali_peak_reservation_counts_the_full_input_and_output_bounds():
    assert upper_bound_cost_cny(
        input_token_bound=32768,
        output_token_bound=2048,
        input_per_million=Decimal("2"),
        output_per_million=Decimal("8"),
    ) == Decimal("0.081920")


def test_restart_preserves_unknown_and_refuses_a_new_attempt(trial_dir):
    path = trial_dir / "trial.json"
    with MemoryTrialBudget(path, price_identity="beijing-model-peak-v1") as budget:
        _reserve(budget)
    with MemoryTrialBudget(path, price_identity="beijing-model-peak-v1") as budget:
        assert budget.state["attempts"][0]["status"] == "RESERVED_OR_UNKNOWN"
        with pytest.raises(ValueError, match="UNRESOLVED"):
            _reserve(budget, "attempt-2")


def test_settlement_retains_attempt_identity_and_rejects_cost_over_reservation(trial_dir):
    path = trial_dir / "trial.json"
    with MemoryTrialBudget(path, price_identity="peak") as budget:
        _reserve(budget)
        with pytest.raises(ValueError, match="EXCEEDS_RESERVATION"):
            budget.settle("attempt-1", conservative_cost_cny=Decimal("2"))
        budget.settle("attempt-1", conservative_cost_cny=Decimal("0.30"))
        with pytest.raises(ValueError, match="DUPLICATE"):
            _reserve(budget)
        _reserve(budget, "attempt-2")
        assert budget.state["attempts"][0]["charged_or_reserved_cny"] == "0.30"


def test_bound_attempt_cannot_settle_without_a_saved_result_identity(trial_dir):
    with MemoryTrialBudget(trial_dir / "trial.json", price_identity="peak") as budget:
        _reserve(budget, request_identity="a" * 64)
        with pytest.raises(ValueError, match="RESULT_IDENTITY"):
            budget.settle("attempt-1", conservative_cost_cny=Decimal("0.30"))
        assert budget.state["attempts"][0]["status"] == "RESERVED_OR_UNKNOWN"
        with pytest.raises(ValueError, match="UNRESOLVED"):
            _reserve(budget, "attempt-2")


@pytest.mark.skipif(os.name != "nt", reason="Windows local sharing behavior")
def test_transient_rename_failure_retries_but_persistent_failure_restores_reservation(
    trial_dir, monkeypatch
):
    real_replace = os.replace
    failures_remaining = 2

    def transient_replace(source, destination):
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise PermissionError(13, "temporarily unavailable", str(destination), 5)
        real_replace(source, destination)

    with MemoryTrialBudget(trial_dir / "trial.json", price_identity="peak") as budget:
        monkeypatch.setattr(os, "replace", transient_replace)
        _reserve(budget)
        assert failures_remaining == 0
        assert budget.state["attempts"][0]["status"] == "RESERVED_OR_UNKNOWN"

        def permanent_failure(_source, destination):
            raise PermissionError(13, "unavailable", str(destination), 5)

        monkeypatch.setattr(os, "replace", permanent_failure)
        with pytest.raises(PermissionError):
            budget.settle("attempt-1", conservative_cost_cny=Decimal("0.3"))
        assert budget.state["attempts"][0]["status"] == "RESERVED_OR_UNKNOWN"


@pytest.mark.parametrize(
    "overrides",
    [
        {"estimated_input_tokens": 1_500_001},
        {"requested_output_tokens": 60_001},
        {"upper_cost_cny": Decimal("30.01")},
    ],
)
def test_over_limit_is_rejected_before_dispatch(trial_dir, overrides):
    with MemoryTrialBudget(trial_dir / "trial.json", price_identity="peak") as budget:
        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(budget, **overrides)
        assert budget.state["attempts"] == []


def test_two_independent_owners_cannot_reserve_against_same_ledger(trial_dir):
    path = trial_dir / "trial.json"
    with MemoryTrialBudget(path, price_identity="peak") as budget:
        with pytest.raises(ValueError, match="ALREADY_OWNED"):
            MemoryTrialBudget(path, price_identity="peak")
    with pytest.raises(ValueError, match="NOT_OWNED"):
        _reserve(budget)


def test_other_process_cannot_open_an_owned_ledger(trial_dir):
    path = trial_dir / "trial.json"
    script = (
        "from pathlib import Path; from benchmarks.evaluation.memory_trial_budget "
        "import MemoryTrialBudget; import sys; "
        "MemoryTrialBudget(Path(sys.argv[1]), price_identity='peak')"
    )
    with MemoryTrialBudget(path, price_identity="peak"):
        result = subprocess.run(
            [sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=10
        )
    assert result.returncode != 0
    assert "MEMORY_TRIAL_ALREADY_OWNED" in result.stderr


def test_legacy_undercounts_cannot_bypass_shared_input_cap(trial_dir):
    with MemoryTrialBudget(trial_dir / "trial.json", price_identity="peak") as budget:
        for index in range(45):
            attempt = f"legacy-{index}"
            _reserve(
                budget, attempt, stage="CALIBRATION", estimated_input_tokens=1,
                upper_cost_cny=Decimal("0.01"),
            )
            budget.settle(attempt, conservative_cost_cny=Decimal("0.01"))
        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(budget, "one-too-many", stage="CALIBRATION", upper_cost_cny=Decimal("0.01"))
        assert len(budget.state["attempts"]) == 45


@pytest.mark.parametrize("stage,limit", [("M4", 40), ("M5", 16)])
def test_stage_call_limit_includes_confirmed_attempts(trial_dir, stage, limit):
    with MemoryTrialBudget(trial_dir / "trial.json", price_identity="peak") as budget:
        for index in range(limit):
            attempt = f"attempt-{index}"
            _reserve(budget, attempt, stage=stage, upper_cost_cny=Decimal("0.01"))
            budget.settle(attempt, conservative_cost_cny=Decimal("0.01"))
        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(budget, "one-too-many", stage=stage, upper_cost_cny=Decimal("0.01"))
