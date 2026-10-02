from decimal import Decimal

import pytest

from benchmarks.evaluation.budget import TrialBudget


def test_unknown_call_survives_restart_and_cannot_exceed_limit(tmp_path):
    path = tmp_path / "budget.json"
    budget = TrialBudget(path, limit_cny=Decimal("5"), price_identity="verified-v1")
    budget.reserve("first", upper_cost_cny=Decimal("4"))
    restored = TrialBudget(path, limit_cny=Decimal("5"), price_identity="verified-v1")
    assert restored.allocated == 4
    with pytest.raises(ValueError, match="EXHAUSTED"):
        restored.reserve("second", upper_cost_cny=Decimal("2"))
    restored.settle("first", cost_cny=Decimal("0.01"))
    restored.reserve("second", upper_cost_cny=Decimal("2"))
    assert restored.allocated == Decimal("2.01")


def test_identity_duplicates_and_invalid_settlement_fail_closed(tmp_path):
    path = tmp_path / "budget.json"
    budget = TrialBudget(path, limit_cny=Decimal("5"), price_identity="verified-v1")
    budget.reserve("first", upper_cost_cny=Decimal("1"))
    with pytest.raises(ValueError, match="DUPLICATE"):
        budget.reserve("first", upper_cost_cny=Decimal("1"))
    with pytest.raises(ValueError, match="EXCEEDS"):
        budget.settle("first", cost_cny=Decimal("2"))
    assert budget.allocated == 1
    with pytest.raises(ValueError, match="IDENTITY"):
        TrialBudget(path, limit_cny=Decimal("5"), price_identity="different")


@pytest.mark.parametrize("value", ["6", "0", "NaN", "Infinity"])
def test_invalid_limit(tmp_path, value):
    with pytest.raises(ValueError):
        TrialBudget(tmp_path / "budget.json", limit_cny=Decimal(value), price_identity="v1")


def test_transient_windows_replace_failure_is_retried(tmp_path, monkeypatch):
    import benchmarks.evaluation.budget as module

    replace = module.os.replace
    calls = []

    def busy_once(source, target):
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError("temporary read handle")
        return replace(source, target)

    monkeypatch.setattr(module.os, "replace", busy_once)
    budget = TrialBudget(tmp_path / "budget.json", limit_cny=Decimal("5"), price_identity="v1")
    assert len(calls) == 2 and budget.allocated == 0


def test_failed_refund_preserves_memory_and_disk_reserve(tmp_path, monkeypatch):
    budget = TrialBudget(tmp_path / "budget.json", limit_cny=Decimal("5"), price_identity="v1")
    budget.reserve("one", upper_cost_cny=Decimal("1"))

    def fail():
        raise PermissionError("busy")

    monkeypatch.setattr(budget, "_save", fail)
    with pytest.raises(PermissionError):
        budget.settle("one", cost_cny=Decimal("0.01"))
    assert budget.allocated == 1
    assert TrialBudget(budget.path, limit_cny=Decimal("5"), price_identity="v1").allocated == 1
