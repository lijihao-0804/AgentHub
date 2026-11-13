import asyncio
from decimal import Decimal

import pytest

from packages.agent_runtime.run_budget_ledger import RunBudgetLedgerError
from scripts.reconcile_case_budget import VerifiedBillLine, reconcile
from tests.integration.test_case_budget_ledger import (
    _create_budget_case,
    _reserve,
    db_resources,
    migrated_database,
    pytestmark,
)

__all__ = ["db_resources", "migrated_database", "pytestmark"]


@pytest.mark.asyncio
async def test_one_invoice_cannot_release_two_holds_even_on_concurrent_import(
    db_resources, monkeypatch
):
    _, factory = db_resources
    case = await _create_budget_case(factory, monkeypatch, limit=Decimal("2"))
    lines = []
    for index, turn in enumerate(case.turns):
        await _reserve(
            factory,
            case,
            index,
            attempt_identity="attempt",
            case_attempt_identity=f"turn-{index}:one",
            amount=Decimal("1"),
        )
        lines.append(
            VerifiedBillLine(
                workspace_id=case.owned.identity.workspace_id,
                case_id=case.owned.identity.case_id,
                turn_index=index,
                agent_run_id=turn.run.id,
                attempt_identity="attempt",
                bill_line_reference=f"synthetic:{case.owned.identity.case_id}:invoice:line-1",
                currency="CNY",
                billed_amount="0.25",
            )
        )

    async def post(line):
        async with factory.begin() as session:
            return await reconcile(session, line)

    results = await asyncio.gather(*(post(line) for line in lines), return_exceptions=True)
    success = next(i for i, result in enumerate(results) if isinstance(result, dict))
    assert results[success]["posted"]
    error = results[1 - success]
    assert isinstance(error, RunBudgetLedgerError) and error.code == "BILL_LINE_ALREADY_ALLOCATED"
    replay = await post(lines[success])
    assert replay["posted"] is False
    assert Decimal(replay["reserved_amount"]) == 1
    assert Decimal(replay["settled_amount"]) == Decimal("0.25")
    assert Decimal(replay["available_amount"]) == Decimal("0.75")
    with pytest.raises(RunBudgetLedgerError) as conflict:
        await post(lines[success].model_copy(update={"billed_amount": "0.24"}))
    assert conflict.value.code == "RUN_BUDGET_SETTLEMENT_CONFLICT"
