import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest

from benchmarks.evaluation.experiments.customer_quality_20261105.budget import (
    ALIYUN_DEEPSEEK_MAX_INPUT_TOKENS,
    ALIYUN_DEEPSEEK_MAX_OUTPUT_TOKENS,
    CONSERVATIVE_RESERVATION_CNY,
    DISPATCHED,
    RESERVED,
    SETTLED,
    UNKNOWN_OUTCOME,
    BudgetExhausted,
    BudgetHalted,
    CallStateConflict,
    InvalidUsage,
    MetadataError,
    ResearchBudgetLedger,
)


def test_concurrent_reservations_never_exceed_30_cny_cap(tmp_path):
    ledger = ResearchBudgetLedger(tmp_path / "concurrent.sqlite3")
    workers = 12
    barrier = Barrier(workers)

    def reserve(index):
        barrier.wait(timeout=10)
        try:
            return ledger.reserve(f"customer-quality/concurrent/{index}", {"case_id": index})
        except BudgetExhausted:
            return None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(reserve, range(workers)))

    successful = [result for result in results if result is not None]
    assert ledger.reservation_cny == CONSERVATIVE_RESERVATION_CNY == Decimal("5.242880")
    assert len(successful) == 5
    summary = ledger.summary()
    assert summary["status_counts"][RESERVED] == 5
    assert summary["occupied_cny"] == Decimal("26.214400")
    assert summary["occupied_cny"] <= summary["cap_cny"] == Decimal("30")
    assert summary["dispatch_marks"] == 0


def test_duplicate_logical_key_reserves_and_dispatches_once(tmp_path):
    db_path = tmp_path / "duplicate.sqlite3"
    ledger = ResearchBudgetLedger(db_path)
    key = "customer-quality/CQ-001/answer"
    metadata = {
        "case_id": "CQ-001",
        "scenario": "customer_service",
        "arm": "A",
    }

    first = ledger.reserve(key, metadata)
    duplicate = ledger.reserve(key, metadata)

    assert first.newly_reserved is True
    assert duplicate.newly_reserved is False
    assert duplicate.status == RESERVED
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_budget_calls").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_budget_audit WHERE event_type = 'RESERVED'"
            ).fetchone()[0]
            == 1
        )

    for prohibited in ("prompt", "api_key"):
        with pytest.raises(MetadataError, match="NON_IDENTITY_METADATA"):
            ledger.reserve(f"customer-quality/prohibited/{prohibited}", {prohibited: "secret"})

    dispatched = ledger.mark_dispatched(key)
    repeated = ledger.reserve(key, metadata)
    assert dispatched.status == DISPATCHED
    assert repeated.status == DISPATCHED
    assert repeated.newly_reserved is False
    with pytest.raises(CallStateConflict, match="LOGICAL_CALL_ALREADY_DISPATCHED"):
        ledger.mark_dispatched(key)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_budget_calls").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_budget_audit WHERE event_type = 'DISPATCHED'"
            ).fetchone()[0]
            == 1
        )


def test_unknown_outcome_keeps_full_reservation_and_halts_globally(tmp_path):
    db_path = tmp_path / "unknown.sqlite3"
    ledger = ResearchBudgetLedger(db_path)
    key = "customer-quality/CQ-002/answer"
    ledger.reserve(key, {"case_id": "CQ-002", "scenario": "customer_service"})
    ledger.mark_dispatched(key)

    unknown = ledger.mark_unknown(key, "TRANSPORT_TIMEOUT")
    summary = ledger.summary()

    assert unknown.status == UNKNOWN_OUTCOME
    assert unknown.reserved_cny == CONSERVATIVE_RESERVATION_CNY
    assert unknown.actual_cny is None
    assert unknown.released_cny == 0
    assert summary["held_reserved_cny"] == CONSERVATIVE_RESERVATION_CNY
    assert summary["occupied_cny"] == CONSERVATIVE_RESERVATION_CNY
    assert summary["remaining_cny"] == Decimal("24.757120")
    assert summary["halted"] is True
    assert summary["halt_reason"].startswith("UNKNOWN_OUTCOME:")

    restarted = ResearchBudgetLedger(db_path)
    with pytest.raises(BudgetHalted, match="BUDGET_HALTED"):
        restarted.reserve("customer-quality/after-unknown", {"case_id": "CQ-003"})
    restarted_summary = restarted.summary()
    assert restarted_summary["occupied_cny"] == CONSERVATIVE_RESERVATION_CNY
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_budget_audit WHERE event_type = 'HALTED'"
            ).fetchone()[0]
            == 1
        )


def test_restart_after_persisted_dispatch_halts_without_new_key_bypass(tmp_path):
    db_path = tmp_path / "restart.sqlite3"
    ledger = ResearchBudgetLedger(db_path)
    key = "customer-quality/CQ-004/answer"
    ledger.reserve(key, {"case_id": "CQ-004"})
    ledger.mark_dispatched(key)

    restarted = ResearchBudgetLedger(db_path)
    summary = restarted.summary()

    assert summary["status_counts"][DISPATCHED] == 1
    assert summary["occupied_cny"] == CONSERVATIVE_RESERVATION_CNY
    assert summary["halted"] is True
    assert summary["halt_reason"] == "STARTUP_UNCONFIRMED_DISPATCH"
    with pytest.raises(BudgetHalted, match="BUDGET_HALTED"):
        restarted.reserve("customer-quality/CQ-004/bypass", {"case_id": "CQ-004"})
    with pytest.raises(BudgetHalted, match="BUDGET_HALTED"):
        restarted.mark_dispatched(key)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT status, dispatched_at
            FROM research_budget_calls
            WHERE logical_call_key = ?
            """,
            (key,),
        ).fetchone()
        assert row[0] == DISPATCHED
        assert row[1] is not None
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_budget_audit WHERE event_type = 'DISPATCHED'"
            ).fetchone()[0]
            == 1
        )


def test_complete_usage_settles_actual_cost_and_releases_difference(tmp_path):
    db_path = tmp_path / "settled.sqlite3"
    ledger = ResearchBudgetLedger(db_path)
    key = "customer-quality/CQ-005/answer"
    ledger.reserve(key, {"case_id": "CQ-005"})
    ledger.mark_dispatched(key)

    settled = ledger.settle(key, 1_000, 200)
    summary = ledger.summary()

    assert settled.status == SETTLED
    assert settled.reserved_cny == CONSERVATIVE_RESERVATION_CNY
    assert settled.actual_cny == Decimal("0.003600")
    assert settled.released_cny == Decimal("5.239280")
    assert summary["held_reserved_cny"] == 0
    assert summary["actual_settled_cny"] == Decimal("0.003600")
    assert summary["occupied_cny"] == Decimal("0.003600")
    assert summary["remaining_cny"] == Decimal("29.996400")
    assert summary["released_cny"] == Decimal("5.239280")
    assert summary["halted"] is False

    with sqlite3.connect(db_path) as connection:
        event = connection.execute(
            """
            SELECT details_json
            FROM research_budget_audit
            WHERE logical_call_key = ? AND event_type = 'SETTLED'
            """,
            (key,),
        ).fetchone()
    assert event is not None
    assert '"actual_cny":"0.003600"' in event[0]
    assert '"released_cny":"5.239280"' in event[0]


def test_explicit_halt_preserves_call_state_and_survives_restart(tmp_path):
    db_path = tmp_path / "global-halt.sqlite3"
    ledger = ResearchBudgetLedger(db_path)
    settled_key = "customer-quality/CQ-005/settled"
    held_key = "customer-quality/CQ-005/held"
    ledger.reserve(settled_key, {"case_id": "CQ-005"})
    ledger.mark_dispatched(settled_key)
    ledger.settle(settled_key, 1_000, 200)
    ledger.reserve(held_key, {"case_id": "CQ-005"})

    ledger.halt("PROVIDER_EVIDENCE_WRITE_FAILED")
    summary = ledger.summary()

    assert summary["halted"] is True
    assert summary["halt_reason"] == "PROVIDER_EVIDENCE_WRITE_FAILED"
    assert summary["status_counts"][SETTLED] == 1
    assert summary["status_counts"][RESERVED] == 1
    assert summary["actual_settled_cny"] == Decimal("0.003600")
    assert summary["held_reserved_cny"] == CONSERVATIVE_RESERVATION_CNY
    assert summary["occupied_cny"] == Decimal("5.246480")

    restarted = ResearchBudgetLedger(db_path)
    assert restarted.summary()["halt_reason"] == "PROVIDER_EVIDENCE_WRITE_FAILED"
    with pytest.raises(BudgetHalted, match="BUDGET_HALTED"):
        restarted.reserve("customer-quality/after-halt", {"case_id": "CQ-006"})
    with pytest.raises(BudgetHalted, match="BUDGET_HALTED"):
        restarted.mark_dispatched(held_key)

    with sqlite3.connect(db_path) as connection:
        rows = dict(
            connection.execute(
                """
                SELECT logical_call_key, status
                FROM research_budget_calls
                WHERE logical_call_key IN (?, ?)
                """,
                (settled_key, held_key),
            ).fetchall()
        )
        assert rows == {settled_key: SETTLED, held_key: RESERVED}
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_budget_audit WHERE event_type = 'HALTED'"
            ).fetchone()[0]
            == 1
        )


def test_invalid_usage_fails_closed_without_releasing_reservation(tmp_path):
    invalid_pairs = (
        (None, 0),
        (True, 0),
        (-1, 0),
        (1.5, 0),
        (ALIYUN_DEEPSEEK_MAX_INPUT_TOKENS + 1, 0),
        (0, ALIYUN_DEEPSEEK_MAX_OUTPUT_TOKENS + 1),
    )

    for index, (input_tokens, output_tokens) in enumerate(invalid_pairs):
        db_path = tmp_path / f"invalid-usage-{index}.sqlite3"
        ledger = ResearchBudgetLedger(db_path)
        key = f"customer-quality/CQ-006/invalid/{index}"
        ledger.reserve(key, {"case_id": "CQ-006"})
        ledger.mark_dispatched(key)

        with pytest.raises(InvalidUsage):
            ledger.settle(key, input_tokens, output_tokens)

        summary = ledger.summary()
        assert summary["status_counts"][UNKNOWN_OUTCOME] == 1
        assert summary["held_reserved_cny"] == CONSERVATIVE_RESERVATION_CNY
        assert summary["occupied_cny"] == CONSERVATIVE_RESERVATION_CNY
        assert summary["actual_settled_cny"] == 0
        assert summary["halted"] is True
        with pytest.raises(BudgetHalted):
            ledger.reserve(f"{key}/bypass", {"case_id": "CQ-006"})
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT status FROM research_budget_calls WHERE logical_call_key = ?",
                (key,),
            ).fetchone()[0] == UNKNOWN_OUTCOME
            assert (
                connection.execute(
                    """
                    SELECT COUNT(*) FROM research_budget_audit
                    WHERE event_type = 'SETTLEMENT_REJECTED'
                    """
                ).fetchone()[0]
                == 1
            )
