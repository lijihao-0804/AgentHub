"""Post a previously verified provider invoice line to one interactive case attempt."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.run_budget_ledger import (
    RunBudgetAttempt,
    RunBudgetLedgerError,
    RunBudgetSettlementEvidence,
)
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.evaluation.case_budget_ledger import CaseBudgetLedger


class VerifiedBillLine(BaseModel):
    """An operator's allocation of one final, globally qualified provider charge."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    case_id: UUID
    turn_index: Annotated[int, Field(strict=True, ge=0)]
    agent_run_id: UUID
    attempt_identity: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    # Include the billing account, invoice period and line ID. Keep identifiers out of logs.
    bill_line_reference: Annotated[str, StringConstraints(pattern=r"^[!-~]{1,256}$")]
    currency: Literal["CNY"]
    billed_amount: Annotated[
        str, StringConstraints(pattern=r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,8})?$")
    ]


async def reconcile(session: AsyncSession, line: VerifiedBillLine) -> dict:
    ledger = CaseBudgetLedger()
    binding = await ledger.get_case_run_binding(
        session, workspace_id=line.workspace_id, case_id=line.case_id, turn_index=line.turn_index
    )
    if binding.agent_run_id != line.agent_run_id:
        raise RunBudgetLedgerError("BILL_RUN_MISMATCH", "The bill allocation and Run differ.")
    # Serialize imports of the same bill line, including allocations racing across tenants.
    digest = hashlib.sha256(line.bill_line_reference.encode("ascii")).hexdigest()
    identity = "provider-invoice-line-v1:" + digest
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": identity}
    )
    previous = (
        await session.execute(
            select(
                RunBudgetAttempt.workspace_id,
                RunBudgetAttempt.run_id,
                RunBudgetAttempt.attempt_identity,
            ).where(RunBudgetAttempt.settlement_evidence_identity == identity)
        )
    ).one_or_none()
    if previous is not None and tuple(previous) != (
        line.workspace_id,
        line.agent_run_id,
        line.attempt_identity,
    ):
        raise RunBudgetLedgerError("BILL_LINE_ALREADY_ALLOCATED", "The bill line was allocated.")
    settled = await ledger.settle(
        session,
        workspace_id=line.workspace_id,
        run_id=line.agent_run_id,
        attempt_identity=line.attempt_identity,
        amount=Decimal(line.billed_amount),
        currency=line.currency,
        evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
        evidence_identity=identity,
    )
    account = await ledger.get_case_account(
        session, workspace_id=line.workspace_id, case_id=line.case_id
    )
    return {
        "posted": settled.created,
        "case_id": str(account.case_id),
        "currency": account.currency,
        "reserved_amount": str(account.reserved_amount),
        "settled_amount": str(account.settled_amount),
        "available_amount": str(account.available_amount),
        "overage_amount": str(account.overage_amount),
    }


async def import_bill(line: VerifiedBillLine) -> dict:
    engine, factory = create_database(get_settings().database_url)
    try:
        async with factory.begin() as session:
            return await reconcile(session, line)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bill_line", type=Path, help="Local JSON allocation of a final, verified bill line"
    )
    args = parser.parse_args()
    try:
        with args.bill_line.open("rb") as file:
            raw = file.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Oversized bill line")
        line = VerifiedBillLine.model_validate_json(raw)
        result = asyncio.run(import_bill(line))
    except RunBudgetLedgerError as exc:
        print(json.dumps({"error": exc.code}))
        return 1
    except (OSError, ValueError, ValidationError, SQLAlchemyError):
        print(json.dumps({"error": "BILL_IMPORT_FAILED"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
