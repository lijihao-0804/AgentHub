"""Single-owner durable limits for the M4/M5 evaluation, never business billing.

Every attempt reserves its full provider cost bound before dispatch. A process
restart preserves unresolved reservations and stops new calls for reconciliation.
"""

from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path


def upper_bound_cost_cny(
    *,
    input_token_bound: int,
    output_token_bound: int,
    input_per_million: Decimal,
    output_per_million: Decimal,
) -> Decimal:
    """Conservative full-price reservation; no cache or free-quota discounts."""
    if (
        type(input_token_bound) is not int
        or input_token_bound <= 0
        or type(output_token_bound) is not int
        or output_token_bound <= 0
        or not input_per_million.is_finite()
        or input_per_million <= 0
        or not output_per_million.is_finite()
        or output_per_million <= 0
    ):
        raise ValueError("INVALID_MEMORY_TRIAL_PRICE")
    return (
        Decimal(input_token_bound) * input_per_million
        + Decimal(output_token_bound) * output_per_million
    ) / Decimal(1_000_000)


class MemoryTrialBudget:
    """One local process owns a trial ledger; concurrent owners are rejected."""

    def __init__(self, path: Path, *, price_identity: str, limit_cny: Decimal = Decimal("30")):
        if not price_identity or not limit_cny.is_finite() or not Decimal(0) < limit_cny <= 30:
            raise ValueError("INVALID_MEMORY_TRIAL_BUDGET")
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = path.with_suffix(path.suffix + ".lock").open("a+b")
        try:
            self._acquire_lock()
            if path.exists():
                self.state = json.loads(path.read_text(encoding="utf-8"))
                if (
                    self.state.get("schema_version") != 1
                    or self.state.get("price_identity") != price_identity
                    or self.state.get("limit_cny") != str(limit_cny)
                    or not isinstance(self.state.get("attempts"), list)
                ):
                    raise ValueError("MEMORY_TRIAL_IDENTITY_MISMATCH")
            else:
                self.state = {
                    "schema_version": 1,
                    "price_identity": price_identity,
                    "limit_cny": str(limit_cny),
                    "attempts": [],
                }
                self._save()
        except BaseException:
            self._lock.close()
            raise

    def close(self) -> None:
        self._lock.close()

    def __enter__(self) -> MemoryTrialBudget:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def reserve(
        self,
        attempt_id: str,
        *,
        stage: str,
        estimated_input_tokens: int,
        requested_output_tokens: int,
        upper_cost_cny: Decimal,
        input_counter: Mapping[str, object] | None = None,
        request_identity: str | None = None,
    ) -> None:
        if self._lock.closed:
            raise ValueError("MEMORY_TRIAL_NOT_OWNED")
        if (
            not attempt_id
            or stage not in {"M4", "M5", "CALIBRATION"}
            or type(estimated_input_tokens) is not int
            or estimated_input_tokens <= 0
            or type(requested_output_tokens) is not int
            or requested_output_tokens <= 0
            or not upper_cost_cny.is_finite()
            or upper_cost_cny <= 0
        ):
            raise ValueError("INVALID_MEMORY_TRIAL_RESERVATION")
        if input_counter is not None and (
            not isinstance(input_counter, Mapping)
            or set(input_counter)
            != {"counter_id", "tokenizer_revision", "thinking_mode", "tokens", "input_limit"}
            or input_counter["counter_id"] != "deepseek_v41_text_v1"
            or input_counter["tokenizer_revision"] != "9f9818127fdfbb60feba204799d617a874b32a86"
            or input_counter["thinking_mode"] != "PROVIDER_DEFAULT_THINKING"
            or type(input_counter["tokens"]) is not int
            or type(input_counter["input_limit"]) is not int
            or not 0 < input_counter["input_limit"] <= 32768
            or not 0 < input_counter["tokens"] <= min(
                input_counter["input_limit"], estimated_input_tokens
            )
        ):
            raise ValueError("INVALID_MEMORY_TRIAL_COUNTER")
        if request_identity is not None and not _valid_identity(request_identity):
            raise ValueError("INVALID_MEMORY_TRIAL_REQUEST_IDENTITY")
        attempts = self.state["attempts"]
        if any(row["status"] == "RESERVED_OR_UNKNOWN" for row in attempts):
            raise ValueError("UNRESOLVED_MEMORY_TRIAL_ATTEMPT")
        if any(row["attempt_id"] == attempt_id for row in attempts):
            raise ValueError("DUPLICATE_MEMORY_TRIAL_ATTEMPT")
        if (
            len(attempts) >= 80
            or (stage == "M4" and sum(row["stage"] == "M4" for row in attempts) >= 40)
            or (stage == "M5" and sum(row["stage"] == "M5" for row in attempts) >= 16)
            # Previous attempts may have used the undercounting legacy
            # estimator. Count every historical request at its full window.
            or sum(max(row["estimated_input_tokens"], 32768) for row in attempts)
            + max(estimated_input_tokens, 32768)
            > 1_500_000
            or sum(row["requested_output_tokens"] for row in attempts) + requested_output_tokens
            > 60_000
            or sum(Decimal(row["charged_or_reserved_cny"]) for row in attempts) + upper_cost_cny
            > Decimal(self.state["limit_cny"])
        ):
            raise ValueError("MEMORY_TRIAL_BUDGET_EXHAUSTED")
        previous = copy.deepcopy(self.state)
        row = {
            "attempt_id": attempt_id,
            "stage": stage,
            "status": "RESERVED_OR_UNKNOWN",
            "estimated_input_tokens": estimated_input_tokens,
            "requested_output_tokens": requested_output_tokens,
            "upper_cost_cny": str(upper_cost_cny),
            "charged_or_reserved_cny": str(upper_cost_cny),
        }
        if input_counter is not None:
            row["input_counter"] = dict(input_counter)
        if request_identity is not None:
            row["request_identity"] = request_identity
        attempts.append(row)
        self._save_or_restore(previous)

    def settle(
        self,
        attempt_id: str,
        *,
        conservative_cost_cny: Decimal,
        result_identity: str | None = None,
    ) -> None:
        if self._lock.closed:
            raise ValueError("MEMORY_TRIAL_NOT_OWNED")
        if not conservative_cost_cny.is_finite() or conservative_cost_cny < 0:
            raise ValueError("INVALID_MEMORY_TRIAL_COST")
        row = next((row for row in self.state["attempts"] if row["attempt_id"] == attempt_id), None)
        if row is None or row["status"] != "RESERVED_OR_UNKNOWN":
            raise ValueError("MEMORY_TRIAL_ATTEMPT_NOT_RESERVED")
        if (row.get("request_identity") is not None and result_identity is None) or (
            result_identity is not None and not _valid_identity(result_identity)
        ):
            raise ValueError("INVALID_MEMORY_TRIAL_RESULT_IDENTITY")
        if conservative_cost_cny > Decimal(row["upper_cost_cny"]):
            raise ValueError("MEMORY_TRIAL_COST_EXCEEDS_RESERVATION")
        previous = copy.deepcopy(self.state)
        row.update(status="SETTLED", charged_or_reserved_cny=str(conservative_cost_cny))
        if result_identity is not None:
            row["result_identity"] = result_identity
        self._save_or_restore(previous)

    def _save_or_restore(self, previous: dict) -> None:
        try:
            self._save()
        except OSError:
            self.state = previous
            raise

    def _save(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Windows may briefly deny an atomic rename while another local process
        # has the file open. Retrying only the local rename is safe: no model
        # request is sent until the reservation has been durably saved.
        for attempt in range(5):
            try:
                os.replace(temporary, self.path)
                break
            except OSError as error:
                if os.name != "nt" or error.winerror not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(0.05)

    def _acquire_lock(self) -> None:
        self._lock.seek(0)
        if os.name == "nt":
            import msvcrt

            if os.fstat(self._lock.fileno()).st_size == 0:
                self._lock.write(b"0")
                self._lock.flush()
            self._lock.seek(0)
            try:
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("MEMORY_TRIAL_ALREADY_OWNED") from None
        else:
            import fcntl

            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("MEMORY_TRIAL_ALREADY_OWNED") from None


def _valid_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
