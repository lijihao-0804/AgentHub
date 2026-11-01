"""Single-owner durable limits for the M4/M5 evaluation, never business billing.

Every attempt reserves its full provider cost bound before dispatch. A process
restart preserves unresolved reservations and stops new calls for reconciliation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

BLOCK_ALL_UNRESOLVED = "BLOCK_ALL_UNRESOLVED"
RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT = "RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT"
LOCAL_SCOPE_ATTEMPT_LIMIT = 9
MAX_SCOPE_ATTEMPT_LIMIT = 10
LOCAL_ROUND_ATTEMPT_LIMIT = 12
MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED = 16
MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED = 18
FINAL_SCOPE_ATTEMPT_LIMIT_AUTHORIZED = 26
FINAL_ROUND_ATTEMPT_LIMIT_AUTHORIZED = 28
_LEGACY_ROUND_ATTEMPTS_USED = 2
_EXPECTED_LEGACY_ATTEMPTS = 9
_EXPECTED_LEGACY_UNRESOLVED_CNY = Decimal("0.009216")
_COUNTER_BASE_KEYS = frozenset(
    {"counter_id", "tokenizer_revision", "thinking_mode", "tokens", "input_limit"}
)
_COUNTER_NON_THINKING_KEYS = _COUNTER_BASE_KEYS | {"request_identity"}


@dataclass(frozen=True, slots=True)
class MemoryTrialReservationReceipt:
    """Immutable identity returned only after a durable reservation save."""

    attempt_id: str
    scope_id: str | None
    request_identity: str | None
    reservation_identity: str
    _capability: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class MemoryTrialResponseReceipt:
    """Verified response evidence bound to its exact reservation and request."""

    attempt_id: str
    scope_id: str
    request_identity: str
    result_identity: str
    conservative_cost_cny: Decimal
    _capability: object = field(repr=False, compare=False)


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
        self._receipt_capability = object()
        self._reservation_receipts: dict[str, MemoryTrialReservationReceipt] = {}
        self._response_receipts: dict[str, MemoryTrialResponseReceipt] = {}
        try:
            self._acquire_lock()
            if path.exists():
                self.state = json.loads(path.read_text(encoding="utf-8"))
                if (
                    self.state.get("schema_version") not in {1, 2}
                    or self.state.get("price_identity") != price_identity
                    or self.state.get("limit_cny") != str(limit_cny)
                    or not isinstance(self.state.get("attempts"), list)
                ):
                    raise ValueError("MEMORY_TRIAL_IDENTITY_MISMATCH")
                if self.state.get("schema_version") == 2:
                    _validate_local_policy_state(self.state)
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

    def enable_independent_scope(
        self,
        scope_id: str,
        *,
        authorization_ref: str,
        now: datetime | None = None,
    ) -> None:
        """Back up and explicitly migrate the verified legacy M4 ledger.

        The migration is one-way. It carries the two already identified current
        round requests into a persisted 2/12 counter, while all nine ledger rows
        remain byte-for-byte equivalent in the saved state.
        """
        self._require_owned()
        _validate_scope_id(scope_id)
        _validate_authorization_ref(authorization_ref)
        if self.state.get("schema_version") == 2:
            policy = self.state.get("local_experiment_policy")
            if (
                isinstance(policy, Mapping)
                and policy.get("mode") == RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT
                and policy.get("active_scope_id") == scope_id
            ):
                return
            raise ValueError("MEMORY_TRIAL_POLICY_ALREADY_ENABLED")
        if self.state.get("schema_version") != 1:
            raise ValueError("MEMORY_TRIAL_POLICY_MIGRATION_UNSUPPORTED")
        _validate_legacy_migration_candidate(self.state)

        changed_at = _timestamp(now)
        previous = copy.deepcopy(self.state)
        self._backup_legacy_ledger()
        self.state["schema_version"] = 2
        self.state["local_experiment_policy"] = {
            "mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
            "round_attempts_baseline": _LEGACY_ROUND_ATTEMPTS_USED,
            "round_attempt_limit": LOCAL_ROUND_ATTEMPT_LIMIT,
            "round_attempts_used": _LEGACY_ROUND_ATTEMPTS_USED,
            "scope_attempt_limit": LOCAL_SCOPE_ATTEMPT_LIMIT,
            "active_scope_id": scope_id,
            "scope_history": [
                {
                    "scope_id": scope_id,
                    "created_at": changed_at,
                    "authorization_ref": authorization_ref,
                }
            ],
            "policy_history": [
                {
                    "previous_mode": BLOCK_ALL_UNRESOLVED,
                    "mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
                    "changed_at": changed_at,
                    "scope_id": scope_id,
                    "authorization_ref": authorization_ref,
                }
            ],
        }
        self._save_or_restore(previous)

    def begin_independent_scope(
        self,
        scope_id: str,
        *,
        authorization_ref: str,
        now: datetime | None = None,
    ) -> None:
        """Select a new scope without resetting the persisted 12-request round."""
        self._require_owned()
        _validate_scope_id(scope_id)
        _validate_authorization_ref(authorization_ref)
        policy = self._local_policy()
        if policy is None or policy.get("mode") != RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT:
            raise ValueError("MEMORY_TRIAL_INDEPENDENT_POLICY_REQUIRED")
        if policy["active_scope_id"] == scope_id:
            return

        previous = copy.deepcopy(self.state)
        known_scopes = {entry["scope_id"] for entry in policy["scope_history"]}
        if scope_id not in known_scopes:
            policy["scope_history"].append(
                {
                    "scope_id": scope_id,
                    "created_at": _timestamp(now),
                    "authorization_ref": authorization_ref,
                }
            )
        policy["active_scope_id"] = scope_id
        policy["policy_history"].append(
            {
                "previous_mode": policy["mode"],
                "mode": policy["mode"],
                "changed_at": _timestamp(now),
                "scope_id": scope_id,
                "authorization_ref": authorization_ref,
            }
        )
        self._save_or_restore(previous)

    def raise_scope_attempt_limit_to_ten(
        self,
        scope_id: str,
        *,
        authorization_ref: str,
        now: datetime | None = None,
    ) -> None:
        """Persist the sole supported 9-to-10 scope-cap increase under the ledger lock."""
        self._require_owned()
        _validate_scope_id(scope_id)
        _validate_authorization_ref(authorization_ref)
        policy = self._local_policy()
        if policy is None or policy.get("mode") != RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT:
            raise ValueError("MEMORY_TRIAL_INDEPENDENT_POLICY_REQUIRED")
        if policy.get("active_scope_id") != scope_id:
            raise ValueError("MEMORY_TRIAL_ACTIVE_SCOPE_MISMATCH")
        if policy.get("scope_attempt_limit") != LOCAL_SCOPE_ATTEMPT_LIMIT:
            raise ValueError("MEMORY_TRIAL_SCOPE_LIMIT_CHANGE_UNSUPPORTED")
        scoped_count = sum(row.get("scope_id") == scope_id for row in self.state["attempts"])
        if scoped_count > LOCAL_SCOPE_ATTEMPT_LIMIT:
            raise ValueError("MEMORY_TRIAL_SCOPE_LIMIT_STATE_INVALID")

        previous = copy.deepcopy(self.state)
        changed_at = _timestamp(now)
        self._backup_scope_limit_change()
        policy["scope_attempt_limit"] = MAX_SCOPE_ATTEMPT_LIMIT
        policy["policy_history"].append(
            {
                "previous_mode": policy["mode"],
                "mode": policy["mode"],
                "changed_at": changed_at,
                "scope_id": scope_id,
                "authorization_ref": authorization_ref,
                "change_type": "SCOPE_ATTEMPT_LIMIT_INCREASE",
                "previous_scope_attempt_limit": LOCAL_SCOPE_ATTEMPT_LIMIT,
                "scope_attempt_limit": MAX_SCOPE_ATTEMPT_LIMIT,
            }
        )
        self._save_or_restore(previous)

    def raise_local_attempt_limits_to_sixteen_eighteen(
        self,
        scope_id: str,
        *,
        authorization_ref: str,
        now: datetime | None = None,
    ) -> None:
        """Apply the authorized 10/12-to-16/18 increase exactly once."""
        self._require_owned()
        _validate_scope_id(scope_id)
        _validate_authorization_ref(authorization_ref)
        policy = self._local_policy()
        if policy is None or policy.get("mode") != RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT:
            raise ValueError("MEMORY_TRIAL_INDEPENDENT_POLICY_REQUIRED")
        if policy.get("active_scope_id") != scope_id:
            raise ValueError("MEMORY_TRIAL_ACTIVE_SCOPE_MISMATCH")
        if (
            policy.get("scope_attempt_limit") != MAX_SCOPE_ATTEMPT_LIMIT
            or policy.get("round_attempt_limit") != LOCAL_ROUND_ATTEMPT_LIMIT
        ):
            raise ValueError("MEMORY_TRIAL_LIMIT_CHANGE_UNSUPPORTED")
        scope_used = sum(row.get("scope_id") == scope_id for row in self.state["attempts"])
        if (
            scope_used > MAX_SCOPE_ATTEMPT_LIMIT
            or policy.get("round_attempts_used", 0) > LOCAL_ROUND_ATTEMPT_LIMIT
        ):
            raise ValueError("MEMORY_TRIAL_LIMIT_STATE_INVALID")

        previous = copy.deepcopy(self.state)
        changed_at = _timestamp(now)
        self._backup_scope_limit_change(
            suffix=".pre-scope-limit-16-round-18.bak",
            conflict_code="MEMORY_TRIAL_LIMIT_BACKUP_CONFLICT",
        )
        policy["scope_attempt_limit"] = MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED
        policy["round_attempt_limit"] = MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED
        policy["policy_history"].append(
            {
                "previous_mode": policy["mode"],
                "mode": policy["mode"],
                "changed_at": changed_at,
                "scope_id": scope_id,
                "authorization_ref": authorization_ref,
                "change_type": "LOCAL_ATTEMPT_LIMIT_INCREASE",
                "previous_scope_attempt_limit": MAX_SCOPE_ATTEMPT_LIMIT,
                "scope_attempt_limit": MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED,
                "previous_round_attempt_limit": LOCAL_ROUND_ATTEMPT_LIMIT,
                "round_attempt_limit": MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED,
            }
        )
        self._save_or_restore(previous)

    def raise_local_attempt_limits_to_twenty_six_twenty_eight(
        self,
        scope_id: str,
        *,
        authorization_ref: str,
        now: datetime | None = None,
    ) -> None:
        """Apply the authorized 16/18-to-26/28 closure increase exactly once."""
        self._require_owned()
        _validate_scope_id(scope_id)
        _validate_authorization_ref(authorization_ref)
        policy = self._local_policy()
        if policy is None or policy.get("mode") != RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT:
            raise ValueError("MEMORY_TRIAL_INDEPENDENT_POLICY_REQUIRED")
        if policy.get("active_scope_id") != scope_id:
            raise ValueError("MEMORY_TRIAL_ACTIVE_SCOPE_MISMATCH")
        if (
            policy.get("scope_attempt_limit") != MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED
            or policy.get("round_attempt_limit") != MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED
        ):
            raise ValueError("MEMORY_TRIAL_LIMIT_CHANGE_UNSUPPORTED")
        scope_used = sum(row.get("scope_id") == scope_id for row in self.state["attempts"])
        if (
            scope_used > MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED
            or policy.get("round_attempts_used", 0) > MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED
        ):
            raise ValueError("MEMORY_TRIAL_LIMIT_STATE_INVALID")

        previous = copy.deepcopy(self.state)
        changed_at = _timestamp(now)
        self._backup_scope_limit_change(
            suffix=".pre-scope-limit-26-round-28.bak",
            conflict_code="MEMORY_TRIAL_FINAL_LIMIT_BACKUP_CONFLICT",
        )
        policy["scope_attempt_limit"] = FINAL_SCOPE_ATTEMPT_LIMIT_AUTHORIZED
        policy["round_attempt_limit"] = FINAL_ROUND_ATTEMPT_LIMIT_AUTHORIZED
        policy["policy_history"].append(
            {
                "previous_mode": policy["mode"],
                "mode": policy["mode"],
                "changed_at": changed_at,
                "scope_id": scope_id,
                "authorization_ref": authorization_ref,
                "change_type": "FINAL_LOCAL_ATTEMPT_LIMIT_INCREASE",
                "previous_scope_attempt_limit": MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED,
                "scope_attempt_limit": FINAL_SCOPE_ATTEMPT_LIMIT_AUTHORIZED,
                "previous_round_attempt_limit": MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED,
                "round_attempt_limit": FINAL_ROUND_ATTEMPT_LIMIT_AUTHORIZED,
            }
        )
        self._save_or_restore(previous)

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
    ) -> MemoryTrialReservationReceipt:
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
        if request_identity is not None and not _valid_identity(request_identity):
            raise ValueError("INVALID_MEMORY_TRIAL_REQUEST_IDENTITY")
        attempts = self.state["attempts"]
        policy = self._local_policy()
        independent = (
            policy is not None
            and policy.get("mode") == RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT
        )
        if input_counter is not None:
            _validate_input_counter(
                input_counter,
                request_identity=request_identity,
                estimated_input_tokens=estimated_input_tokens,
                allow_non_thinking=independent,
            )
        if not independent and any(row["status"] == "RESERVED_OR_UNKNOWN" for row in attempts):
            raise ValueError("UNRESOLVED_MEMORY_TRIAL_ATTEMPT")
        if any(row["attempt_id"] == attempt_id for row in attempts):
            raise ValueError("DUPLICATE_MEMORY_TRIAL_ATTEMPT")
        if independent:
            if request_identity is None:
                raise ValueError("MEMORY_TRIAL_REQUEST_IDENTITY_REQUIRED")
            if any(row.get("request_identity") == request_identity for row in attempts):
                raise ValueError("DUPLICATE_MEMORY_TRIAL_REQUEST")
            if policy["round_attempts_used"] >= policy["round_attempt_limit"]:
                raise ValueError("MEMORY_TRIAL_BUDGET_EXHAUSTED")
            # This cap is shared by every scope in the independent experiment;
            # selecting another scope cannot replenish its nine dispatches.
            scope_attempts = sum(row.get("scope_id") is not None for row in attempts)
            if scope_attempts >= policy["scope_attempt_limit"]:
                raise ValueError("MEMORY_TRIAL_BUDGET_EXHAUSTED")
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
        if independent:
            row["scope_id"] = policy["active_scope_id"]
            policy["round_attempts_used"] += 1
        attempts.append(row)
        self._save_or_restore(previous)
        receipt = MemoryTrialReservationReceipt(
            attempt_id=attempt_id,
            scope_id=row.get("scope_id"),
            request_identity=row.get("request_identity"),
            reservation_identity=_reservation_identity(row),
            _capability=self._receipt_capability,
        )
        self._reservation_receipts[attempt_id] = receipt
        return receipt

    def issue_response_receipt(
        self,
        reservation_receipt: MemoryTrialReservationReceipt,
        *,
        result_identity: str,
        conservative_cost_cny: Decimal,
    ) -> MemoryTrialResponseReceipt:
        """Bind verified response evidence to a live reservation in this owner."""
        if self._lock.closed:
            raise ValueError("MEMORY_TRIAL_NOT_OWNED")
        if not isinstance(reservation_receipt, MemoryTrialReservationReceipt):
            raise ValueError("MEMORY_TRIAL_RESERVATION_RECEIPT_INVALID")
        attempt_id = reservation_receipt.attempt_id
        row = next(
            (item for item in self.state["attempts"] if item.get("attempt_id") == attempt_id),
            None,
        )
        if (
            self._reservation_receipts.get(attempt_id) is not reservation_receipt
            or attempt_id in self._response_receipts
            or reservation_receipt._capability is not self._receipt_capability
            or row is None
            or row.get("status") != "RESERVED_OR_UNKNOWN"
            or reservation_receipt.scope_id is None
            or reservation_receipt.scope_id != row.get("scope_id")
            or reservation_receipt.scope_id != (self._local_policy() or {}).get("active_scope_id")
            or reservation_receipt.request_identity != row.get("request_identity")
            or reservation_receipt.reservation_identity != _reservation_identity(row)
        ):
            raise ValueError("MEMORY_TRIAL_RESPONSE_RESERVATION_MISMATCH")
        if (
            not _valid_identity(result_identity)
            or not isinstance(conservative_cost_cny, Decimal)
            or not conservative_cost_cny.is_finite()
            or conservative_cost_cny < 0
            or conservative_cost_cny > Decimal(row["upper_cost_cny"])
        ):
            raise ValueError("INVALID_MEMORY_TRIAL_RESPONSE_RECEIPT")
        receipt = MemoryTrialResponseReceipt(
            attempt_id=attempt_id,
            scope_id=reservation_receipt.scope_id,
            request_identity=reservation_receipt.request_identity,
            result_identity=result_identity,
            conservative_cost_cny=conservative_cost_cny,
            _capability=self._receipt_capability,
        )
        self._response_receipts[attempt_id] = receipt
        return receipt

    def settle(
        self,
        attempt_id: str,
        *,
        conservative_cost_cny: Decimal,
        result_identity: str | None = None,
        reservation_receipt: MemoryTrialReservationReceipt | None = None,
        response_receipt: MemoryTrialResponseReceipt | None = None,
    ) -> None:
        if self._lock.closed:
            raise ValueError("MEMORY_TRIAL_NOT_OWNED")
        if not conservative_cost_cny.is_finite() or conservative_cost_cny < 0:
            raise ValueError("INVALID_MEMORY_TRIAL_COST")
        row = next((row for row in self.state["attempts"] if row["attempt_id"] == attempt_id), None)
        if row is None or row["status"] != "RESERVED_OR_UNKNOWN":
            raise ValueError("MEMORY_TRIAL_ATTEMPT_NOT_RESERVED")
        if self._local_policy() is not None and row.get("scope_id") is None:
            raise ValueError("MEMORY_TRIAL_LEGACY_UNKNOWN_IMMUTABLE")
        if row.get("scope_id") is not None:
            policy = self._local_policy()
            if policy is None or policy.get("active_scope_id") != row.get("scope_id"):
                raise ValueError("MEMORY_TRIAL_SETTLEMENT_SCOPE_MISMATCH")
            if not isinstance(reservation_receipt, MemoryTrialReservationReceipt):
                raise ValueError("MEMORY_TRIAL_RESERVATION_RECEIPT_REQUIRED")
            if not isinstance(response_receipt, MemoryTrialResponseReceipt):
                raise ValueError("MEMORY_TRIAL_RESPONSE_RECEIPT_REQUIRED")
            if (
                self._reservation_receipts.get(attempt_id) is not reservation_receipt
                or self._response_receipts.get(attempt_id) is not response_receipt
                or reservation_receipt._capability is not self._receipt_capability
                or response_receipt._capability is not self._receipt_capability
                or reservation_receipt.attempt_id != attempt_id
                or reservation_receipt.scope_id != row.get("scope_id")
                or reservation_receipt.request_identity != row.get("request_identity")
                or reservation_receipt.reservation_identity != _reservation_identity(row)
                or response_receipt.attempt_id != attempt_id
                or response_receipt.scope_id != row.get("scope_id")
                or response_receipt.request_identity != row.get("request_identity")
                or response_receipt.result_identity != result_identity
                or response_receipt.conservative_cost_cny != conservative_cost_cny
            ):
                raise ValueError("MEMORY_TRIAL_SETTLEMENT_RECEIPT_MISMATCH")
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
        self._reservation_receipts.pop(attempt_id, None)
        self._response_receipts.pop(attempt_id, None)

    def _save_or_restore(self, previous: dict) -> None:
        try:
            self._save()
        except BaseException:
            self.state = previous
            raise

    def _local_policy(self) -> dict | None:
        policy = self.state.get("local_experiment_policy")
        return policy if isinstance(policy, dict) else None

    def _require_owned(self) -> None:
        if self._lock.closed:
            raise ValueError("MEMORY_TRIAL_NOT_OWNED")

    def _backup_legacy_ledger(self) -> None:
        if not self.path.is_file():
            raise ValueError("MEMORY_TRIAL_BACKUP_SOURCE_MISSING")
        source_bytes = self.path.read_bytes()
        backup_path = self.path.with_name(self.path.name + ".pre-independent-scope.bak")
        if backup_path.exists():
            if not backup_path.is_file() or backup_path.read_bytes() != source_bytes:
                raise ValueError("MEMORY_TRIAL_BACKUP_CONFLICT")
            return
        try:
            with backup_path.open("xb") as backup:
                backup.write(source_bytes)
                backup.flush()
                os.fsync(backup.fileno())
        except FileExistsError:
            if not backup_path.is_file() or backup_path.read_bytes() != source_bytes:
                raise ValueError("MEMORY_TRIAL_BACKUP_CONFLICT") from None

    def _backup_scope_limit_change(
        self,
        *,
        suffix: str = ".pre-scope-limit-10.bak",
        conflict_code: str = "MEMORY_TRIAL_SCOPE_LIMIT_BACKUP_CONFLICT",
    ) -> None:
        if not self.path.is_file():
            raise ValueError("MEMORY_TRIAL_BACKUP_SOURCE_MISSING")
        source_bytes = self.path.read_bytes()
        backup_path = self.path.with_name(self.path.name + suffix)
        if backup_path.exists():
            if not backup_path.is_file() or backup_path.read_bytes() != source_bytes:
                raise ValueError(conflict_code)
            return
        try:
            with backup_path.open("xb") as backup:
                backup.write(source_bytes)
                backup.flush()
                os.fsync(backup.fileno())
        except FileExistsError:
            if not backup_path.is_file() or backup_path.read_bytes() != source_bytes:
                raise ValueError(conflict_code) from None

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


def _reservation_identity(row: Mapping[str, object]) -> str:
    """Hash the immutable request and reservation fields persisted in the ledger."""
    identity_payload = {
        key: row.get(key)
        for key in (
            "attempt_id",
            "scope_id",
            "request_identity",
            "stage",
            "estimated_input_tokens",
            "requested_output_tokens",
            "upper_cost_cny",
            "input_counter",
        )
    }
    encoded = json.dumps(
        identity_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_legacy_migration_candidate(state: Mapping[str, object]) -> None:
    attempts = state.get("attempts")
    if (
        state.get("price_identity") != "aliyun-beijing-deepseek-v4.1-flash-1017-peak-2-8-cny-v1"
        or state.get("limit_cny") != "30"
        or not isinstance(attempts, list)
        or len(attempts) != _EXPECTED_LEGACY_ATTEMPTS
    ):
        raise ValueError("MEMORY_TRIAL_POLICY_LEGACY_BASELINE_MISMATCH")
    if any(not isinstance(row, Mapping) for row in attempts):
        raise ValueError("MEMORY_TRIAL_POLICY_LEGACY_BASELINE_MISMATCH")
    status_counts: dict[str, int] = {}
    for row in attempts:
        status = row.get("status")
        if not isinstance(status, str):
            raise ValueError("MEMORY_TRIAL_POLICY_LEGACY_BASELINE_MISMATCH")
        status_counts[status] = status_counts.get(status, 0) + 1
    if status_counts != {"SETTLED": 8, "RESERVED_OR_UNKNOWN": 1}:
        raise ValueError("MEMORY_TRIAL_POLICY_LEGACY_BASELINE_MISMATCH")

    identified = [row for row in attempts if row.get("request_identity") is not None]
    unresolved = [row for row in attempts if row.get("status") == "RESERVED_OR_UNKNOWN"]
    if (
        len(identified) != _LEGACY_ROUND_ATTEMPTS_USED
        or len(unresolved) != 1
        or not _valid_identity(unresolved[0].get("request_identity"))
        or Decimal(unresolved[0].get("charged_or_reserved_cny", "NaN"))
        != _EXPECTED_LEGACY_UNRESOLVED_CNY
        or Decimal(unresolved[0].get("upper_cost_cny", "NaN")) != _EXPECTED_LEGACY_UNRESOLVED_CNY
    ):
        raise ValueError("MEMORY_TRIAL_POLICY_LEGACY_BASELINE_MISMATCH")

    total_cny = sum(
        (Decimal(row.get("charged_or_reserved_cny", "NaN")) for row in attempts),
        Decimal(0),
    )
    full_window_input = sum(max(row.get("estimated_input_tokens", 0), 32768) for row in attempts)
    requested_output = sum(row.get("requested_output_tokens", 0) for row in attempts)
    if total_cny != Decimal("0.315316") or full_window_input != 294_912 or requested_output != 8640:
        raise ValueError("MEMORY_TRIAL_POLICY_LEGACY_BASELINE_MISMATCH")

    attempt_ids = [row.get("attempt_id") for row in attempts]
    request_ids = [row.get("request_identity") for row in identified]
    if (
        any(not isinstance(item, str) or not item for item in attempt_ids)
        or len(set(attempt_ids)) != len(attempt_ids)
        or len(set(request_ids)) != len(request_ids)
        or any(not _valid_identity(item) for item in request_ids)
        or any(row.get("scope_id") is not None for row in attempts)
    ):
        raise ValueError("MEMORY_TRIAL_POLICY_LEGACY_BASELINE_MISMATCH")


def _validate_local_policy_state(state: Mapping[str, object]) -> None:
    policy = state.get("local_experiment_policy")
    attempts = state.get("attempts")
    if (
        not isinstance(policy, Mapping)
        or not isinstance(attempts, list)
        or policy.get("mode") != RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT
        or policy.get("round_attempts_baseline") != _LEGACY_ROUND_ATTEMPTS_USED
        or (policy.get("scope_attempt_limit"), policy.get("round_attempt_limit"))
        not in {
            (LOCAL_SCOPE_ATTEMPT_LIMIT, LOCAL_ROUND_ATTEMPT_LIMIT),
            (MAX_SCOPE_ATTEMPT_LIMIT, LOCAL_ROUND_ATTEMPT_LIMIT),
            (MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED, MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED),
            (FINAL_SCOPE_ATTEMPT_LIMIT_AUTHORIZED, FINAL_ROUND_ATTEMPT_LIMIT_AUTHORIZED),
        }
        or type(policy.get("round_attempts_used")) is not int
        or not _LEGACY_ROUND_ATTEMPTS_USED
        <= policy["round_attempts_used"]
        <= policy["round_attempt_limit"]
        or not isinstance(policy.get("scope_history"), list)
        or not isinstance(policy.get("policy_history"), list)
        or not policy["scope_history"]
        or not policy["policy_history"]
    ):
        raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")

    scopes: set[str] = set()
    for scope in policy["scope_history"]:
        if (
            not isinstance(scope, Mapping)
            or not _valid_scope_id(scope.get("scope_id"))
            or scope["scope_id"] in scopes
            or not _valid_timestamp(scope.get("created_at"))
            or not _valid_authorization_ref(scope.get("authorization_ref"))
        ):
            raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")
        scopes.add(scope["scope_id"])
    if policy.get("active_scope_id") not in scopes:
        raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")

    history = policy["policy_history"]
    limit_changes = []
    for index, event in enumerate(history):
        if (
            not isinstance(event, Mapping)
            or event.get("mode") != RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT
            or event.get("previous_mode")
            != (
                BLOCK_ALL_UNRESOLVED
                if index == 0
                else RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT
            )
            or event.get("scope_id") not in scopes
            or not _valid_timestamp(event.get("changed_at"))
            or not _valid_authorization_ref(event.get("authorization_ref"))
        ):
            raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")
        change_type = event.get("change_type")
        if change_type is None:
            if any(
                key in event
                for key in (
                    "previous_scope_attempt_limit",
                    "scope_attempt_limit",
                    "previous_round_attempt_limit",
                    "round_attempt_limit",
                )
            ):
                raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")
        elif change_type == "SCOPE_ATTEMPT_LIMIT_INCREASE":
            if (
                event.get("previous_scope_attempt_limit") != LOCAL_SCOPE_ATTEMPT_LIMIT
                or event.get("scope_attempt_limit") != MAX_SCOPE_ATTEMPT_LIMIT
                or "previous_round_attempt_limit" in event
                or "round_attempt_limit" in event
            ):
                raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")
            limit_changes.append(change_type)
        elif change_type == "LOCAL_ATTEMPT_LIMIT_INCREASE":
            if (
                event.get("previous_scope_attempt_limit") != MAX_SCOPE_ATTEMPT_LIMIT
                or event.get("scope_attempt_limit") != MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED
                or event.get("previous_round_attempt_limit") != LOCAL_ROUND_ATTEMPT_LIMIT
                or event.get("round_attempt_limit") != MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED
            ):
                raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")
            limit_changes.append(change_type)
        elif change_type == "FINAL_LOCAL_ATTEMPT_LIMIT_INCREASE":
            if (
                event.get("previous_scope_attempt_limit") != MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED
                or event.get("scope_attempt_limit") != FINAL_SCOPE_ATTEMPT_LIMIT_AUTHORIZED
                or event.get("previous_round_attempt_limit") != MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED
                or event.get("round_attempt_limit") != FINAL_ROUND_ATTEMPT_LIMIT_AUTHORIZED
            ):
                raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")
            limit_changes.append(change_type)
        else:
            raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")

    expected_changes = {
        (LOCAL_SCOPE_ATTEMPT_LIMIT, LOCAL_ROUND_ATTEMPT_LIMIT): [],
        (MAX_SCOPE_ATTEMPT_LIMIT, LOCAL_ROUND_ATTEMPT_LIMIT): ["SCOPE_ATTEMPT_LIMIT_INCREASE"],
        (MAX_SCOPE_ATTEMPT_LIMIT_AUTHORIZED, MAX_ROUND_ATTEMPT_LIMIT_AUTHORIZED): [
            "SCOPE_ATTEMPT_LIMIT_INCREASE",
            "LOCAL_ATTEMPT_LIMIT_INCREASE",
        ],
        (FINAL_SCOPE_ATTEMPT_LIMIT_AUTHORIZED, FINAL_ROUND_ATTEMPT_LIMIT_AUTHORIZED): [
            "SCOPE_ATTEMPT_LIMIT_INCREASE",
            "LOCAL_ATTEMPT_LIMIT_INCREASE",
            "FINAL_LOCAL_ATTEMPT_LIMIT_INCREASE",
        ],
    }[(policy["scope_attempt_limit"], policy["round_attempt_limit"])]
    if limit_changes != expected_changes:
        raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")

    scoped_attempts = 0
    seen_attempt_ids: set[str] = set()
    seen_request_ids: set[str] = set()
    for row in attempts:
        if not isinstance(row, Mapping):
            raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")
        attempt_id = row.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id or attempt_id in seen_attempt_ids:
            raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")
        seen_attempt_ids.add(attempt_id)
        request_identity = row.get("request_identity")
        if request_identity is not None:
            if not _valid_identity(request_identity) or request_identity in seen_request_ids:
                raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")
            seen_request_ids.add(request_identity)
        scope_id = row.get("scope_id")
        if scope_id is not None:
            if scope_id not in scopes:
                raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")
            scoped_attempts += 1
    if policy["round_attempts_used"] != _LEGACY_ROUND_ATTEMPTS_USED + scoped_attempts:
        raise ValueError("MEMORY_TRIAL_POLICY_STATE_INVALID")


def _validate_scope_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 128
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("INVALID_MEMORY_TRIAL_SCOPE_ID")


def _valid_scope_id(value: object) -> bool:
    try:
        _validate_scope_id(value)
    except ValueError:
        return False
    return True


def _validate_authorization_ref(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 and character not in "\t" for character in value)
    ):
        raise ValueError("INVALID_MEMORY_TRIAL_AUTHORIZATION_REF")


def _valid_authorization_ref(value: object) -> bool:
    try:
        _validate_authorization_ref(value)
    except ValueError:
        return False
    return True


def _timestamp(value: datetime | None) -> str:
    current = datetime.now(UTC) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("INVALID_MEMORY_TRIAL_TIMESTAMP")
    return current.astimezone(UTC).isoformat(timespec="seconds")


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _validate_input_counter(
    value: Mapping[str, object],
    *,
    request_identity: str | None,
    estimated_input_tokens: int,
    allow_non_thinking: bool,
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("INVALID_MEMORY_TRIAL_COUNTER")
    mode = value.get("thinking_mode")
    expected_keys = _COUNTER_NON_THINKING_KEYS if mode == "NON_THINKING" else _COUNTER_BASE_KEYS
    if (
        set(value) != expected_keys
        or value.get("counter_id") != "deepseek_v41_text_v1"
        or value.get("tokenizer_revision") != "9f9818127fdfbb60feba204799d617a874b32a86"
        or mode not in {"PROVIDER_DEFAULT_THINKING", "NON_THINKING"}
        or (mode == "NON_THINKING" and not allow_non_thinking)
        or type(value.get("tokens")) is not int
        or type(value.get("input_limit")) is not int
        or not 0 < value["input_limit"] <= 32768
        or not 0 < value["tokens"] <= min(value["input_limit"], estimated_input_tokens)
    ):
        raise ValueError("INVALID_MEMORY_TRIAL_COUNTER")
    if mode == "NON_THINKING" and (
        not _valid_identity(request_identity)
        or value.get("request_identity") != request_identity
        or not _valid_identity(value.get("request_identity"))
    ):
        raise ValueError("INVALID_MEMORY_TRIAL_COUNTER_REQUEST_IDENTITY")
