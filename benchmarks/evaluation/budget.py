"""Durable reservations for a serial local trial; never refunds unknown attempts.

The caller owns verified prices and token bounds. This ledger is not a provider
billing limit or a replacement for ContextBudgetPolicy. One process owns a ledger.
"""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from pathlib import Path


class TrialBudget:
    def __init__(self, path: Path, *, limit_cny: Decimal, price_identity: str):
        if not limit_cny.is_finite() or not 0 < limit_cny <= 50 or not price_identity:
            raise ValueError("INVALID_TRIAL_BUDGET")
        self.path = path
        if path.exists():
            self.state = json.loads(path.read_text(encoding="utf-8"))
            if (
                self.state["limit_cny"] != str(limit_cny)
                or self.state["price_identity"] != price_identity
            ):
                raise ValueError("BUDGET_IDENTITY_MISMATCH")
        else:
            self.state = {
                "schema_version": 1,
                "currency": "CNY",
                "limit_cny": str(limit_cny),
                "price_identity": price_identity,
                "attempts": [],
            }
            self._save()

    @property
    def allocated(self) -> Decimal:
        return sum(
            (Decimal(row["charged_or_reserved_cny"]) for row in self.state["attempts"]), Decimal(0)
        )

    def increase_limit(self, limit_cny: Decimal, *, authorization_ref: str) -> None:
        """Explicit authorization changes the ceiling, never past attempt accounting."""
        previous_limit = Decimal(self.state["limit_cny"])
        if (
            not limit_cny.is_finite()
            or not previous_limit < limit_cny <= 50
            or not authorization_ref.strip()
        ):
            raise ValueError("INVALID_BUDGET_INCREASE")
        previous = json.loads(json.dumps(self.state))
        self.state["limit_cny"] = str(limit_cny)
        self.state.setdefault("limit_changes", []).append(
            {
                "previous_cny": str(previous_limit),
                "new_cny": str(limit_cny),
                "authorization_ref": authorization_ref,
            }
        )
        try:
            self._save()
        except OSError:
            self.state = previous
            raise

    def reserve(self, attempt_id: str, *, upper_cost_cny: Decimal) -> None:
        if not attempt_id or any(row["attempt_id"] == attempt_id for row in self.state["attempts"]):
            raise ValueError("DUPLICATE_OR_EMPTY_ATTEMPT")
        if not upper_cost_cny.is_finite() or upper_cost_cny <= 0:
            raise ValueError("INVALID_RESERVATION")
        if self.allocated + upper_cost_cny > Decimal(self.state["limit_cny"]):
            raise ValueError("TRIAL_BUDGET_EXHAUSTED")
        self.state["attempts"].append(
            {
                "attempt_id": attempt_id,
                "status": "RESERVED_OR_UNKNOWN",
                "charged_or_reserved_cny": str(upper_cost_cny),
                "upper_cost_cny": str(upper_cost_cny),
            }
        )
        self._save()  # Durable before network dispatch; crash keeps the full reserve.

    def settle(self, attempt_id: str, *, cost_cny: Decimal) -> None:
        if not cost_cny.is_finite() or cost_cny < 0:
            raise ValueError("INVALID_OBSERVED_COST")
        row = next((r for r in self.state["attempts"] if r["attempt_id"] == attempt_id), None)
        if row is None or row["status"] != "RESERVED_OR_UNKNOWN":
            raise ValueError("ATTEMPT_NOT_RESERVED")
        if cost_cny > Decimal(row["upper_cost_cny"]):
            raise ValueError("OBSERVED_COST_EXCEEDS_RESERVATION_STOP_TRIAL")
        previous = dict(row)
        row.update(status="SETTLED_ESTIMATE", charged_or_reserved_cny=str(cost_cny))
        try:
            self._save()
        except OSError:
            row.update(previous)  # A failed refund must retain the full reserve in memory too.
            raise

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, self.path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)  # Bounded Windows scanner/read-handle contention.
