"""Financial and attempt limits apply before the evaluation sends a request."""

import copy
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from benchmarks.evaluation.memory_trial_budget import (
    BLOCK_ALL_UNRESOLVED,
    LOCAL_ROUND_ATTEMPT_LIMIT,
    LOCAL_SCOPE_ATTEMPT_LIMIT,
    MAX_SCOPE_ATTEMPT_LIMIT,
    MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED,
    MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED,
    RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
    MemoryTrialBudget,
    upper_bound_cost_cny,
)

LEGACY_PRICE_IDENTITY = "aliyun-beijing-deepseek-v4.1-flash-1017-peak-2-8-cny-v1"
AUTHORIZATION_REF = "user instruction 2026-10-28: independent local trial requests"


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
    return budget.reserve(attempt, **params)


def _seed_verified_legacy_ledger(path: Path) -> tuple[dict, bytes]:
    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        output_bounds = (64, 2048, 2048, 2048, 2048, 64, 64, 128, 128)
        for index, output_bound in enumerate(output_bounds):
            attempt = f"legacy-{index + 1}"
            stage = "CALIBRATION" if index < 3 else "M4"
            request_identity = None
            upper_cost = Decimal("0.01")
            if index == 7:
                request_identity = "a" * 64
                upper_cost = Decimal("0.2361")
            if index == 8:
                request_identity = "b" * 64
                upper_cost = Decimal("0.009216")
            _reserve(
                budget,
                attempt,
                stage=stage,
                estimated_input_tokens=100,
                requested_output_tokens=output_bound,
                upper_cost_cny=upper_cost,
                **({"request_identity": request_identity} if request_identity else {}),
            )
            if index == 8:
                continue
            settled_cost = upper_cost if index == 7 else Decimal("0.01")
            budget.settle(
                attempt,
                conservative_cost_cny=settled_cost,
                **({"result_identity": "c" * 64} if request_identity else {}),
            )
    state = json.loads(path.read_text(encoding="utf-8"))
    return state, path.read_bytes()


def _enable_independent_scope(budget, scope_id="m4-independent-1"):
    budget.enable_independent_scope(
        scope_id,
        authorization_ref=AUTHORIZATION_REF,
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )


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


def test_explicit_scope_migration_backs_up_ledger_and_preserves_all_attempt_rows(trial_dir):
    path = trial_dir / "trial.json"
    old_state, old_bytes = _seed_verified_legacy_ledger(path)
    old_attempts = copy.deepcopy(old_state["attempts"])

    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget)
        policy = budget.state["local_experiment_policy"]
        backup = path.with_name(path.name + ".pre-independent-scope.bak")

        assert backup.read_bytes() == old_bytes
        assert budget.state["attempts"] == old_attempts
        assert budget.state["schema_version"] == 2
        assert policy["mode"] == RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT
        assert policy["round_attempts_baseline"] == 2
        assert policy["round_attempts_used"] == 2
        assert policy["round_attempt_limit"] == LOCAL_ROUND_ATTEMPT_LIMIT == 12
        assert policy["scope_attempt_limit"] == LOCAL_SCOPE_ATTEMPT_LIMIT == 9
        assert policy["policy_history"][0]["previous_mode"] == BLOCK_ALL_UNRESOLVED
        assert policy["policy_history"][0]["scope_id"] == "m4-independent-1"
        assert policy["policy_history"][0]["authorization_ref"] == AUTHORIZATION_REF
        assert budget.state["attempts"][-1]["status"] == "RESERVED_OR_UNKNOWN"
        assert budget.state["attempts"][-1]["charged_or_reserved_cny"] == "0.009216"


def test_independent_scope_allows_distinct_identity_and_never_replays_unknown(trial_dir):
    path = trial_dir / "trial.json"
    _seed_verified_legacy_ledger(path)
    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget)
        original_unknown = copy.deepcopy(budget.state["attempts"][-1])
        with pytest.raises(ValueError, match="LEGACY_UNKNOWN_IMMUTABLE"):
            budget.settle(
                "legacy-9",
                conservative_cost_cny=Decimal("0.009216"),
                result_identity="e" * 64,
            )
        _reserve(
            budget,
            "new-independent",
            request_identity="d" * 64,
            input_counter={
                "counter_id": "deepseek_v41_text_v1",
                "tokenizer_revision": "9f9818127fdfbb60feba204799d617a874b32a86",
                "thinking_mode": "PROVIDER_DEFAULT_THINKING",
                "tokens": 100,
                "input_limit": 32768,
            },
        )
        assert budget.state["attempts"][-1]["status"] == "RESERVED_OR_UNKNOWN"
        assert budget.state["attempts"][-1]["scope_id"] == "m4-independent-1"
        assert budget.state["attempts"][-1]["input_counter"]["thinking_mode"] == (
            "PROVIDER_DEFAULT_THINKING"
        )
        assert budget.state["attempts"][-2] == original_unknown
        assert budget.state["local_experiment_policy"]["round_attempts_used"] == 3
        with pytest.raises(ValueError, match="DUPLICATE_MEMORY_TRIAL_REQUEST"):
            _reserve(budget, "new-id-same-request", request_identity="b" * 64)
        with pytest.raises(ValueError, match="DUPLICATE_MEMORY_TRIAL_REQUEST"):
            _reserve(budget, "duplicate-settled-request", request_identity="a" * 64)
        with pytest.raises(ValueError, match="DUPLICATE_MEMORY_TRIAL_ATTEMPT"):
            _reserve(budget, "legacy-9", request_identity="f" * 64)
        with pytest.raises(ValueError, match="REQUEST_IDENTITY_REQUIRED"):
            _reserve(budget, "missing-identity")
        assert len(budget.state["attempts"]) == 10


def test_scope_limit_migrates_only_from_nine_to_ten_and_preserves_attempt_rows(trial_dir):
    path = trial_dir / "trial.json"
    _seed_verified_legacy_ledger(path)
    authorization_ref = "User authorization 2026-10-28: raise this experiment scope cap 9 to 10 only"
    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget, "scope-seven")
        original_unknown = copy.deepcopy(budget.state["attempts"][-1])
        for index in range(7):
            _reserve(
                budget,
                f"scope-seven-{index}",
                request_identity=f"{index + 16:064x}",
            )
        assert budget.state["local_experiment_policy"]["round_attempts_used"] == 9
        before_attempts = copy.deepcopy(budget.state["attempts"])
        before_policy = copy.deepcopy(budget.state["local_experiment_policy"])
        before_bytes = path.read_bytes()

        budget.raise_scope_attempt_limit_to_ten(
            "scope-seven",
            authorization_ref=authorization_ref,
            now=datetime(2026, 10, 28, 3, tzinfo=UTC),
        )

        policy = budget.state["local_experiment_policy"]
        assert budget.state["attempts"] == before_attempts
        assert budget.state["attempts"][8] == original_unknown
        assert budget.state["attempts"][8]["status"] == "RESERVED_OR_UNKNOWN"
        assert budget.state["attempts"][8]["charged_or_reserved_cny"] == "0.009216"
        assert policy["round_attempts_used"] == 9
        assert policy["round_attempt_limit"] == before_policy["round_attempt_limit"] == 12
        assert policy["scope_attempt_limit"] == MAX_SCOPE_ATTEMPT_LIMIT == 10
        assert policy["active_scope_id"] == "scope-seven"
        assert policy["policy_history"][:-1] == before_policy["policy_history"]
        assert policy["policy_history"][-1] == {
            "previous_mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
            "mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
            "changed_at": "2026-10-28T03:00:00+00:00",
            "scope_id": "scope-seven",
            "authorization_ref": authorization_ref,
            "change_type": "SCOPE_ATTEMPT_LIMIT_INCREASE",
            "previous_scope_attempt_limit": LOCAL_SCOPE_ATTEMPT_LIMIT,
            "scope_attempt_limit": MAX_SCOPE_ATTEMPT_LIMIT,
        }
        backup = path.with_name(path.name + ".pre-scope-limit-10.bak")
        assert backup.read_bytes() == before_bytes

        _reserve(budget, "scope-seven-eighth", request_identity=f"{0x20:064x}")
        _reserve(budget, "scope-seven-ninth", request_identity=f"{0x21:064x}")
        _reserve(budget, "scope-seven-tenth", request_identity=f"{0x22:064x}")
        assert policy["round_attempts_used"] == 12
        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(budget, "scope-seven-eleventh", request_identity=f"{0x23:064x}")

        with pytest.raises(ValueError, match="SCOPE_LIMIT_CHANGE_UNSUPPORTED"):
            budget.raise_scope_attempt_limit_to_ten(
                "scope-seven",
                authorization_ref=authorization_ref,
            )


@pytest.mark.parametrize(
    "target_scope",
    ["another-scope", ""],
)
def test_scope_limit_migration_rejects_wrong_scope_without_changing_ledger(
    trial_dir, target_scope
):
    path = trial_dir / "trial.json"
    _seed_verified_legacy_ledger(path)
    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget, "scope-current")
        before = copy.deepcopy(budget.state)
        before_bytes = path.read_bytes()
        with pytest.raises(ValueError):
            budget.raise_scope_attempt_limit_to_ten(
                target_scope,
                authorization_ref=AUTHORIZATION_REF,
            )
        assert budget.state == before
        assert path.read_bytes() == before_bytes
        assert not path.with_name(path.name + ".pre-scope-limit-10.bak").exists()


def test_scope_and_round_limits_migrate_10_12_to_16_18_without_touching_costs_or_unknowns(
    trial_dir,
):
    path = trial_dir / "trial.json"
    _seed_verified_legacy_ledger(path)
    scope_id = "scope-current"
    authorization_ref = "User instruction 2026-10-29: increase local scope/round caps 10/12 to 16/18"
    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget, scope_id)
        budget.raise_scope_attempt_limit_to_ten(
            scope_id,
            authorization_ref="earlier authorized scope limit 9 to 10",
            now=datetime(2026, 10, 29, tzinfo=UTC),
        )
        for index in range(8):
            _reserve(budget, f"scope-existing-{index}", request_identity=f"{0x40 + index:064x}")
        assert budget.state["local_experiment_policy"]["scope_attempt_limit"] == 10
        assert budget.state["local_experiment_policy"]["round_attempt_limit"] == 12
        assert budget.state["local_experiment_policy"]["round_attempts_used"] == 10
        before_attempts = copy.deepcopy(budget.state["attempts"])
        before_unknowns = [
            copy.deepcopy(row)
            for row in budget.state["attempts"]
            if row["status"] == "RESERVED_OR_UNKNOWN"
        ]
        before_cost = sum(
            (Decimal(row["charged_or_reserved_cny"]) for row in budget.state["attempts"]),
            Decimal(0),
        )
        before_bytes = path.read_bytes()

        budget.raise_local_attempt_limits_to_sixteen_eighteen(
            scope_id,
            authorization_ref=authorization_ref,
            now=datetime(2026, 10, 29, 1, tzinfo=UTC),
        )

        policy = budget.state["local_experiment_policy"]
        assert policy["scope_attempt_limit"] == MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED == 16
        assert policy["round_attempt_limit"] == MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED == 18
        assert policy["round_attempts_used"] == 10
        assert budget.state["attempts"] == before_attempts
        assert [
            row for row in budget.state["attempts"] if row["status"] == "RESERVED_OR_UNKNOWN"
        ] == before_unknowns
        assert sum(
            (Decimal(row["charged_or_reserved_cny"]) for row in budget.state["attempts"]),
            Decimal(0),
        ) == before_cost
        assert budget.state["limit_cny"] == "30"
        assert policy["policy_history"][-1] == {
            "previous_mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
            "mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
            "changed_at": "2026-10-29T01:00:00+00:00",
            "scope_id": scope_id,
            "authorization_ref": authorization_ref,
            "change_type": "LOCAL_ATTEMPT_LIMIT_INCREASE",
            "previous_scope_attempt_limit": 10,
            "scope_attempt_limit": 16,
            "previous_round_attempt_limit": 12,
            "round_attempt_limit": 18,
        }
        assert path.with_name(path.name + ".pre-scope-limit-16-round-18.bak").read_bytes() == before_bytes

        for index in range(8):
            _reserve(
                budget,
                f"scope-expanded-{index}",
                request_identity=f"{0x60 + index:064x}",
            )
        assert sum(row.get("scope_id") == scope_id for row in budget.state["attempts"]) == 16
        assert policy["round_attempts_used"] == 18
        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(budget, "scope-seventeenth", request_identity=f"{0x70:064x}")
        with pytest.raises(ValueError, match="LIMIT_CHANGE_UNSUPPORTED"):
            budget.raise_local_attempt_limits_to_sixteen_eighteen(
                scope_id,
                authorization_ref=authorization_ref,
            )


def test_scoped_settlement_requires_active_scope_request_and_response_receipts(trial_dir):
    path = trial_dir / "trial.json"
    _seed_verified_legacy_ledger(path)
    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget, "scope-alpha")
        reservation = _reserve(
            budget,
            "alpha-request",
            request_identity="d" * 64,
            input_counter={
                "counter_id": "deepseek_v41_text_v1",
                "tokenizer_revision": "9f9818127fdfbb60feba204799d617a874b32a86",
                "thinking_mode": "NON_THINKING",
                "tokens": 22,
                "input_limit": 4096,
                "request_identity": "d" * 64,
            },
        )
        response_receipt = budget.issue_response_receipt(
            reservation,
            result_identity="e" * 64,
            conservative_cost_cny=Decimal("0.000076"),
        )
        with pytest.raises(ValueError, match="RESPONSE_RECEIPT_REQUIRED"):
            budget.settle(
                reservation.attempt_id,
                conservative_cost_cny=Decimal("0.000076"),
                result_identity="e" * 64,
                reservation_receipt=reservation,
            )
        row = next(row for row in budget.state["attempts"] if row["attempt_id"] == "alpha-request")
        assert row["status"] == "RESERVED_OR_UNKNOWN"
        assert row["charged_or_reserved_cny"] == row["upper_cost_cny"]

        budget.begin_independent_scope("scope-bravo", authorization_ref=AUTHORIZATION_REF)
        with pytest.raises(ValueError, match="SETTLEMENT_SCOPE_MISMATCH"):
            budget.settle(
                reservation.attempt_id,
                conservative_cost_cny=Decimal("0"),
                result_identity="f" * 64,
                reservation_receipt=reservation,
                response_receipt=response_receipt,
            )
        row = next(row for row in budget.state["attempts"] if row["attempt_id"] == "alpha-request")
        assert row["status"] == "RESERVED_OR_UNKNOWN"
        assert row["charged_or_reserved_cny"] == row["upper_cost_cny"]

        budget.begin_independent_scope("scope-alpha", authorization_ref=AUTHORIZATION_REF)
        beta_reservation = _reserve(
            budget,
            "beta-request",
            request_identity="9" * 64,
        )
        beta_response = budget.issue_response_receipt(
            beta_reservation,
            result_identity="f" * 64,
            conservative_cost_cny=Decimal("0.30"),
        )
        with pytest.raises(ValueError, match="SETTLEMENT_RECEIPT_MISMATCH"):
            budget.settle(
                reservation.attempt_id,
                conservative_cost_cny=Decimal("0.30"),
                result_identity="f" * 64,
                reservation_receipt=reservation,
                response_receipt=beta_response,
            )
        budget.settle(
            reservation.attempt_id,
            conservative_cost_cny=Decimal("0.000076"),
            result_identity="e" * 64,
            reservation_receipt=reservation,
            response_receipt=response_receipt,
        )
        row = next(row for row in budget.state["attempts"] if row["attempt_id"] == "alpha-request")
        assert row["status"] == "SETTLED"
        assert row["charged_or_reserved_cny"] == "0.000076"
        assert row["request_identity"] == "d" * 64


def test_scope_switch_does_not_reset_round_or_nine_attempt_experiment_limit(trial_dir):
    path = trial_dir / "trial.json"
    _seed_verified_legacy_ledger(path)
    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget)
        for index in range(LOCAL_SCOPE_ATTEMPT_LIMIT):
            _reserve(
                budget,
                f"scope-one-{index}",
                request_identity=f"{index + 1:064x}",
            )
        assert budget.state["local_experiment_policy"]["round_attempts_used"] == 11
        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(budget, "scope-one-too-many", request_identity="f" * 64)

        budget.begin_independent_scope(
            "m4-independent-2",
            authorization_ref=AUTHORIZATION_REF,
            now=datetime(2026, 10, 28, 1, tzinfo=UTC),
        )
        assert budget.state["local_experiment_policy"]["round_attempts_used"] == 11
        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(budget, "scope-two-cannot-reset-nine", request_identity="e" * 64)

        budget.begin_independent_scope(
            "m4-independent-3",
            authorization_ref=AUTHORIZATION_REF,
            now=datetime(2026, 10, 28, 2, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(budget, "cannot-reset-round", request_identity="1" * 64)
        assert budget.state["local_experiment_policy"]["round_attempts_used"] == 11


def test_ledger_wide_cost_and_token_caps_include_all_nine_legacy_rows(trial_dir):
    path = trial_dir / "trial.json"
    _seed_verified_legacy_ledger(path)
    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget)
        assert sum(
            (Decimal(row["charged_or_reserved_cny"]) for row in budget.state["attempts"]),
            Decimal(0),
        ) == Decimal("0.315316")
        assert (
            sum(max(row["estimated_input_tokens"], 32768) for row in budget.state["attempts"])
            == 9 * 32768
        )
        assert sum(row["requested_output_tokens"] for row in budget.state["attempts"]) == 8640

        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(
                budget,
                "over-global-cny",
                request_identity="2" * 64,
                upper_cost_cny=Decimal("29.684685"),
            )
        assert len(budget.state["attempts"]) == 9

        _reserve(
            budget,
            "exact-global-cny",
            request_identity="3" * 64,
            estimated_input_tokens=1_205_088,
            requested_output_tokens=1,
            upper_cost_cny=Decimal("29.684684"),
        )
        assert sum(
            (Decimal(row["charged_or_reserved_cny"]) for row in budget.state["attempts"]),
            Decimal(0),
        ) == Decimal("30.000000")
        assert (
            sum(max(row["estimated_input_tokens"], 32768) for row in budget.state["attempts"])
            == 1_500_000
        )

    input_path = trial_dir / "input-cap.json"
    _seed_verified_legacy_ledger(input_path)
    with MemoryTrialBudget(input_path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget)
        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(
                budget,
                "over-global-input",
                request_identity="4" * 64,
                estimated_input_tokens=1_205_089,
            )

    output_path = trial_dir / "output-cap.json"
    _seed_verified_legacy_ledger(output_path)
    with MemoryTrialBudget(output_path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget)
        with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
            _reserve(
                budget,
                "over-global-output",
                request_identity="5" * 64,
                requested_output_tokens=51_361,
            )


def test_non_thinking_counter_is_explicit_and_bound_to_request_identity(trial_dir):
    path = trial_dir / "trial.json"
    _seed_verified_legacy_ledger(path)
    non_thinking_identity = "d" * 64
    counter = {
        "counter_id": "deepseek_v41_text_v1",
        "tokenizer_revision": "9f9818127fdfbb60feba204799d617a874b32a86",
        "thinking_mode": "NON_THINKING",
        "tokens": 1024,
        "input_limit": 8192,
        "request_identity": non_thinking_identity,
    }
    default_path = trial_dir / "default-policy.json"
    _seed_verified_legacy_ledger(default_path)
    with MemoryTrialBudget(default_path, price_identity=LEGACY_PRICE_IDENTITY) as default_budget:
        with pytest.raises(ValueError, match="INVALID_MEMORY_TRIAL_COUNTER"):
            _reserve(
                default_budget,
                "non-thinking-with-default-policy",
                request_identity="f" * 64,
                input_counter=counter,
            )

    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget)
        _reserve(
            budget,
            "non-thinking-request",
            request_identity=non_thinking_identity,
            estimated_input_tokens=32768,
            input_counter=counter,
        )
        stored = budget.state["attempts"][-1]["input_counter"]
        assert stored["thinking_mode"] == "NON_THINKING"
        assert stored["request_identity"] == non_thinking_identity

        with pytest.raises(ValueError, match="COUNTER_REQUEST_IDENTITY"):
            _reserve(
                budget,
                "non-thinking-wrong-identity",
                request_identity="e" * 64,
                estimated_input_tokens=32768,
                input_counter=counter,
            )


def test_policy_migration_and_reservation_failures_restore_disk_and_memory(trial_dir, monkeypatch):
    path = trial_dir / "trial.json"
    old_state, old_bytes = _seed_verified_legacy_ledger(path)
    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:

        def fail_save():
            raise OSError("simulated crash before atomic replace")

        monkeypatch.setattr(budget, "_save", fail_save)
        with pytest.raises(OSError, match="simulated crash"):
            _enable_independent_scope(budget)
        assert budget.state == old_state
        assert path.read_bytes() == old_bytes
        assert path.with_name(path.name + ".pre-independent-scope.bak").read_bytes() == old_bytes

    with MemoryTrialBudget(path, price_identity=LEGACY_PRICE_IDENTITY) as budget:
        _enable_independent_scope(budget)
        before_state = copy.deepcopy(budget.state)
        before_bytes = path.read_bytes()
        monkeypatch.setattr(budget, "_save", fail_save)
        with pytest.raises(OSError, match="simulated crash"):
            _reserve(budget, "save-fails", request_identity="d" * 64)
        assert budget.state == before_state
        assert path.read_bytes() == before_bytes


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
                budget,
                attempt,
                stage="CALIBRATION",
                estimated_input_tokens=1,
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
