"""Persistent SQLite budget ledger for the T04 customer-quality research run.

This module is intentionally separate from the platform run-budget implementation.  It
reserves against the provider's full published token envelope before dispatch, retains
unconfirmed reservations, and halts all new dispatches after an ambiguous outcome.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, localcontext
from pathlib import Path
from types import MappingProxyType

ALIYUN_DEEPSEEK_MAX_INPUT_TOKENS = 1_048_576
ALIYUN_DEEPSEEK_MAX_OUTPUT_TOKENS = 393_216
ALIYUN_DEEPSEEK_INPUT_CNY_PER_MILLION = Decimal("2")
ALIYUN_DEEPSEEK_OUTPUT_CNY_PER_MILLION = Decimal("8")
CONSERVATIVE_RESERVATION_CNY = Decimal("5.242880")

RESERVED = "RESERVED"
DISPATCHED = "DISPATCHED"
SETTLED = "SETTLED"
UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"

METADATA_IDENTITY_KEYS = frozenset(
    {
        "agent_run_id",
        "arm",
        "case_id",
        "model",
        "phase",
        "provider",
        "request_id",
        "scenario",
        "thread_id",
        "turn_id",
        "variant",
    }
)

_CURRENCY_QUANTUM = Decimal("0.000001")
_MILLION_TOKENS = Decimal(1_000_000)
_LOGICAL_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/=-]{0,255}\Z")
_REASON_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/=-]{0,127}\Z")
_STATUSES = (RESERVED, DISPATCHED, SETTLED, UNKNOWN_OUTCOME)
type MetadataValue = str | int | bool | None


class ResearchBudgetError(RuntimeError):
    """Base error for a rejected or halted research-budget operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class BudgetConfigurationError(ResearchBudgetError):
    """The existing ledger configuration differs from the requested configuration."""


class BudgetHalted(ResearchBudgetError):
    """The ledger is globally halted and cannot reserve or dispatch another call."""


class BudgetExhausted(ResearchBudgetError):
    """The next conservative reservation would exceed the CNY cap."""


class LogicalCallConflict(ResearchBudgetError):
    """A logical call key was reused with different immutable identity metadata."""


class CallNotFound(ResearchBudgetError):
    """The logical call key does not exist in this ledger."""


class CallStateConflict(ResearchBudgetError):
    """The requested transition is invalid for the call's durable state."""


class InvalidUsage(ResearchBudgetError):
    """Usage is missing or outside the provider's complete conservative envelope."""


class MetadataError(ResearchBudgetError):
    """Metadata contains a non-identity field or unsupported value."""


@dataclass(frozen=True, slots=True)
class Reservation:
    """A durable snapshot returned by every logical-call state transition."""

    logical_call_key: str
    status: str
    reserved_cny: Decimal
    actual_cny: Decimal | None
    released_cny: Decimal
    metadata: Mapping[str, MetadataValue]
    newly_reserved: bool = False


class ResearchBudgetLedger:
    """A CNY 30 research ledger with atomic reserve, dispatch, and settlement."""

    def __init__(
        self,
        db_path: str | Path,
        cap_cny: Decimal = Decimal("30"),
        input_price_per_million_cny: Decimal = ALIYUN_DEEPSEEK_INPUT_CNY_PER_MILLION,
        output_price_per_million_cny: Decimal = ALIYUN_DEEPSEEK_OUTPUT_CNY_PER_MILLION,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cap_cny = _validated_decimal("cap_cny", cap_cny, positive=True)
        self.input_price_per_million_cny = _validated_decimal(
            "input_price_per_million_cny",
            input_price_per_million_cny,
            positive=True,
        )
        self.output_price_per_million_cny = _validated_decimal(
            "output_price_per_million_cny",
            output_price_per_million_cny,
            positive=True,
        )
        if self.cap_cny > Decimal("30"):
            raise ValueError("cap_cny must not exceed the authorized CNY 30 research cap")
        if (
            self.input_price_per_million_cny < ALIYUN_DEEPSEEK_INPUT_CNY_PER_MILLION
            or self.output_price_per_million_cny < ALIYUN_DEEPSEEK_OUTPUT_CNY_PER_MILLION
        ):
            raise ValueError("prices must not undercut the authorized Aliyun DeepSeek peak prices")
        self.reservation_cny = _conservative_reservation_cny(
            self.input_price_per_million_cny,
            self.output_price_per_million_cny,
        )
        self._initialize()
        self._halt_if_startup_state_is_unconfirmed()

    def reserve(
        self,
        logical_call_key: str,
        metadata: Mapping[str, MetadataValue] | None = None,
    ) -> Reservation:
        """Atomically reserve the full provider envelope for one logical call.

        A repeated key returns the existing row and never creates another reservation.
        Only a caller that wins ``mark_dispatched`` may cross the provider boundary.
        """

        key = _validated_logical_call_key(logical_call_key)
        metadata_json = _validated_metadata_json(metadata)
        with self._write_transaction() as connection:
            config = self._config(connection)
            if bool(config["halted"]):
                raise BudgetHalted(
                    "BUDGET_HALTED",
                    f"Research ledger is halted: {config['halt_reason']}",
                )

            existing = self._call(connection, key)
            if existing is not None:
                if existing["metadata_json"] != metadata_json:
                    raise LogicalCallConflict(
                        "LOGICAL_CALL_CONFLICT",
                        f"Logical call key {key!r} has different identity metadata",
                    )
                return _reservation(existing)

            occupied_cny = _occupied_cny(connection)
            if occupied_cny + self.reservation_cny > self.cap_cny:
                raise BudgetExhausted(
                    "CNY_BUDGET_EXHAUSTED",
                    (
                        f"Reserving {self.reservation_cny} CNY would exceed "
                        f"{self.cap_cny} CNY"
                    ),
                )

            now = _timestamp()
            connection.execute(
                """
                INSERT INTO research_budget_calls (
                    logical_call_key,
                    metadata_json,
                    status,
                    reserved_cny,
                    input_upper_tokens,
                    output_upper_tokens,
                    input_tokens,
                    output_tokens,
                    actual_cny,
                    released_cny,
                    terminal_reason,
                    created_at,
                    updated_at,
                    dispatched_at,
                    settled_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL)
                """,
                (
                    key,
                    metadata_json,
                    RESERVED,
                    _money_text(self.reservation_cny),
                    ALIYUN_DEEPSEEK_MAX_INPUT_TOKENS,
                    ALIYUN_DEEPSEEK_MAX_OUTPUT_TOKENS,
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                key,
                "RESERVED",
                None,
                RESERVED,
                {
                    "reserved_cny": _money_text(self.reservation_cny),
                    "input_upper_tokens": ALIYUN_DEEPSEEK_MAX_INPUT_TOKENS,
                    "output_upper_tokens": ALIYUN_DEEPSEEK_MAX_OUTPUT_TOKENS,
                },
            )
            created = self._call(connection, key)
            if created is None:
                raise RuntimeError("reservation was not persisted")
            return _reservation(created, newly_reserved=True)

    def mark_dispatched(self, logical_call_key: str) -> Reservation:
        """Persist the dispatch intent before the caller makes the provider request."""

        key = _validated_logical_call_key(logical_call_key)
        with self._write_transaction() as connection:
            config = self._config(connection)
            if bool(config["halted"]):
                raise BudgetHalted(
                    "BUDGET_HALTED",
                    f"Research ledger is halted: {config['halt_reason']}",
                )
            row = self._required_call(connection, key)
            if row["status"] != RESERVED:
                raise CallStateConflict(
                    "LOGICAL_CALL_ALREADY_DISPATCHED",
                    f"Logical call {key!r} is {row['status']}, not {RESERVED}",
                )
            now = _timestamp()
            connection.execute(
                """
                UPDATE research_budget_calls
                SET status = ?, dispatched_at = ?, updated_at = ?
                WHERE logical_call_key = ? AND status = ?
                """,
                (DISPATCHED, now, now, key, RESERVED),
            )
            self._append_event(
                connection,
                key,
                "DISPATCHED",
                RESERVED,
                DISPATCHED,
                {},
            )
            return _reservation(self._required_call(connection, key))

    def settle(
        self,
        logical_call_key: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Reservation:
        """Settle complete provider usage and release the unused reservation."""

        key = _validated_logical_call_key(logical_call_key)
        failure: InvalidUsage | None = None
        with self._write_transaction() as connection:
            row = self._required_call(connection, key)
            invalid_code = _usage_validation_code(input_tokens, output_tokens)
            if row["status"] == SETTLED:
                if invalid_code is not None:
                    raise InvalidUsage(
                        invalid_code,
                        f"Invalid usage for {key!r}: {invalid_code}",
                    )
                if (
                    row["input_tokens"] == input_tokens
                    and row["output_tokens"] == output_tokens
                ):
                    return _reservation(row)
                raise CallStateConflict(
                    "LOGICAL_CALL_ALREADY_SETTLED",
                    f"Logical call {key!r} is already settled with different usage",
                )

            if invalid_code is not None:
                self._mark_unknown_in_transaction(connection, row, invalid_code)
                self._append_event(
                    connection,
                    key,
                    "SETTLEMENT_REJECTED",
                    row["status"],
                    UNKNOWN_OUTCOME,
                    {"code": invalid_code},
                )
                failure = InvalidUsage(invalid_code, f"Invalid usage for {key!r}: {invalid_code}")
            elif row["status"] != DISPATCHED:
                raise CallStateConflict(
                    "LOGICAL_CALL_NOT_DISPATCHED",
                    f"Logical call {key!r} is {row['status']}, not {DISPATCHED}",
                )
            else:
                actual_cny = self._actual_cny(input_tokens, output_tokens)
                reserved_cny = Decimal(row["reserved_cny"])
                if actual_cny > reserved_cny:
                    invalid_code = "USAGE_EXCEEDS_RESERVED_ENVELOPE"
                    self._mark_unknown_in_transaction(connection, row, invalid_code)
                    self._append_event(
                        connection,
                        key,
                        "SETTLEMENT_REJECTED",
                        row["status"],
                        UNKNOWN_OUTCOME,
                        {"code": invalid_code},
                    )
                    failure = InvalidUsage(
                        invalid_code,
                        f"Actual cost for {key!r} exceeds its reservation",
                    )
                else:
                    released_cny = reserved_cny - actual_cny
                    now = _timestamp()
                    connection.execute(
                        """
                        UPDATE research_budget_calls
                        SET status = ?,
                            input_tokens = ?,
                            output_tokens = ?,
                            actual_cny = ?,
                            released_cny = ?,
                            updated_at = ?,
                            settled_at = ?
                        WHERE logical_call_key = ? AND status = ?
                        """,
                        (
                            SETTLED,
                            input_tokens,
                            output_tokens,
                            _money_text(actual_cny),
                            _money_text(released_cny),
                            now,
                            now,
                            key,
                            DISPATCHED,
                        ),
                    )
                    self._append_event(
                        connection,
                        key,
                        "SETTLED",
                        DISPATCHED,
                        SETTLED,
                        {
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "actual_cny": _money_text(actual_cny),
                            "released_cny": _money_text(released_cny),
                        },
                    )
                    row = self._required_call(connection, key)
        if failure is not None:
            raise failure
        return _reservation(row)

    def mark_unknown(
        self,
        logical_call_key: str,
        reason: str = "UNRECONCILED_OUTCOME",
    ) -> Reservation:
        """Keep the full reservation and halt all future provider dispatch."""

        key = _validated_logical_call_key(logical_call_key)
        reason_code = _validated_reason(reason)
        with self._write_transaction() as connection:
            row = self._required_call(connection, key)
            if row["status"] == SETTLED:
                raise CallStateConflict(
                    "LOGICAL_CALL_ALREADY_SETTLED",
                    f"Settled logical call {key!r} cannot become unknown",
                )
            if row["status"] == UNKNOWN_OUTCOME:
                return _reservation(row)
            self._mark_unknown_in_transaction(connection, row, reason_code)
            return _reservation(self._required_call(connection, key))

    def summary(self) -> dict[str, object]:
        """Return a consistent aggregate without changing the ledger."""

        with self._read_transaction() as connection:
            config = self._config(connection)
            counts = {status: 0 for status in _STATUSES}
            held_reserved_cny = Decimal(0)
            actual_settled_cny = Decimal(0)
            released_cny = Decimal(0)
            for row in connection.execute(
                """
                SELECT status, reserved_cny, actual_cny, released_cny
                FROM research_budget_calls
                ORDER BY created_at, logical_call_key
                """
            ):
                counts[row["status"]] += 1
                if row["status"] == SETTLED:
                    actual_settled_cny += Decimal(row["actual_cny"])
                    released_cny += Decimal(row["released_cny"])
                else:
                    held_reserved_cny += Decimal(row["reserved_cny"])
            occupied_cny = held_reserved_cny + actual_settled_cny
            dispatch_marks = connection.execute(
                """
                SELECT COUNT(*) FROM research_budget_audit
                WHERE event_type = 'DISPATCHED'
                """
            ).fetchone()[0]
            audit_events = connection.execute(
                "SELECT COUNT(*) FROM research_budget_audit"
            ).fetchone()[0]
            cap_cny = Decimal(config["cap_cny"])
            return {
                "cap_cny": cap_cny,
                "occupied_cny": occupied_cny,
                "remaining_cny": cap_cny - occupied_cny,
                "held_reserved_cny": held_reserved_cny,
                "actual_settled_cny": actual_settled_cny,
                "released_cny": released_cny,
                "status_counts": counts,
                "halted": bool(config["halted"]),
                "halt_reason": config["halt_reason"],
                "dispatch_marks": dispatch_marks,
                "audit_events": audit_events,
            }

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_budget_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cap_cny TEXT NOT NULL,
                    input_price_per_million_cny TEXT NOT NULL,
                    output_price_per_million_cny TEXT NOT NULL,
                    input_upper_tokens INTEGER NOT NULL,
                    output_upper_tokens INTEGER NOT NULL,
                    halted INTEGER NOT NULL CHECK (halted IN (0, 1)),
                    halt_reason TEXT,
                    initialized_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS research_budget_calls (
                    logical_call_key TEXT PRIMARY KEY,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('RESERVED', 'DISPATCHED', 'SETTLED', 'UNKNOWN_OUTCOME')
                    ),
                    reserved_cny TEXT NOT NULL,
                    input_upper_tokens INTEGER NOT NULL,
                    output_upper_tokens INTEGER NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    actual_cny TEXT,
                    released_cny TEXT,
                    terminal_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    dispatched_at TEXT,
                    settled_at TEXT
                );

                CREATE TABLE IF NOT EXISTS research_budget_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    logical_call_key TEXT,
                    event_type TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_research_budget_calls_status
                ON research_budget_calls (status);

                CREATE INDEX IF NOT EXISTS ix_research_budget_audit_call
                ON research_budget_audit (logical_call_key, id);
                """
            )
            now = _timestamp()
            connection.execute(
                """
                INSERT OR IGNORE INTO research_budget_config (
                    id,
                    cap_cny,
                    input_price_per_million_cny,
                    output_price_per_million_cny,
                    input_upper_tokens,
                    output_upper_tokens,
                    halted,
                    halt_reason,
                    initialized_at
                ) VALUES (1, ?, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (
                    _money_text(self.cap_cny),
                    _decimal_text(self.input_price_per_million_cny),
                    _decimal_text(self.output_price_per_million_cny),
                    ALIYUN_DEEPSEEK_MAX_INPUT_TOKENS,
                    ALIYUN_DEEPSEEK_MAX_OUTPUT_TOKENS,
                    now,
                ),
            )
            config = self._config(connection)
            expected = (
                self.cap_cny,
                self.input_price_per_million_cny,
                self.output_price_per_million_cny,
                ALIYUN_DEEPSEEK_MAX_INPUT_TOKENS,
                ALIYUN_DEEPSEEK_MAX_OUTPUT_TOKENS,
            )
            actual = (
                Decimal(config["cap_cny"]),
                Decimal(config["input_price_per_million_cny"]),
                Decimal(config["output_price_per_million_cny"]),
                config["input_upper_tokens"],
                config["output_upper_tokens"],
            )
            if actual != expected:
                raise BudgetConfigurationError(
                    "BUDGET_CONFIGURATION_DRIFT",
                    "SQLite ledger configuration differs from constructor arguments",
                )

    def _halt_if_startup_state_is_unconfirmed(self) -> None:
        with self._write_transaction() as connection:
            config = self._config(connection)
            rows = list(
                connection.execute(
                    """
                    SELECT logical_call_key, status
                    FROM research_budget_calls
                    WHERE status IN ('DISPATCHED', 'UNKNOWN_OUTCOME')
                    ORDER BY created_at, logical_call_key
                    """
                )
            )
            if not rows or bool(config["halted"]):
                return
            reason = (
                "STARTUP_UNCONFIRMED_DISPATCH"
                if any(row["status"] == DISPATCHED for row in rows)
                else "STARTUP_UNKNOWN_OUTCOME"
            )
            connection.execute(
                """
                UPDATE research_budget_config
                SET halted = 1, halt_reason = ?
                WHERE id = 1
                """,
                (reason,),
            )
            self._append_event(
                connection,
                None,
                "HALTED",
                None,
                None,
                {
                    "reason": reason,
                    "calls": [
                        {
                            "logical_call_key": row["logical_call_key"],
                            "status": row["status"],
                        }
                        for row in rows
                    ],
                },
            )

    def _mark_unknown_in_transaction(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        code: str,
    ) -> None:
        key = row["logical_call_key"]
        reason = f"UNKNOWN_OUTCOME:{key}:{code}"
        now = _timestamp()
        connection.execute(
            """
            UPDATE research_budget_calls
            SET status = ?, terminal_reason = ?, updated_at = ?
            WHERE logical_call_key = ? AND status IN ('RESERVED', 'DISPATCHED')
            """,
            (UNKNOWN_OUTCOME, code, now, key),
        )
        connection.execute(
            """
            UPDATE research_budget_config
            SET halted = 1,
                halt_reason = COALESCE(halt_reason, ?)
            WHERE id = 1
            """,
            (reason,),
        )
        self._append_event(
            connection,
            key,
            "UNKNOWN_OUTCOME",
            row["status"],
            UNKNOWN_OUTCOME,
            {"code": code},
        )
        self._append_event(
            connection,
            key,
            "HALTED",
            None,
            None,
            {"reason": reason},
        )

    def _actual_cny(self, input_tokens: int, output_tokens: int) -> Decimal:
        with localcontext() as context:
            context.prec = 28
            input_cost = (
                Decimal(input_tokens) * self.input_price_per_million_cny / _MILLION_TOKENS
            )
            output_cost = (
                Decimal(output_tokens) * self.output_price_per_million_cny / _MILLION_TOKENS
            )
            return (input_cost + output_cost).quantize(
                _CURRENCY_QUANTUM,
                rounding=ROUND_CEILING,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _config(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM research_budget_config WHERE id = 1"
        ).fetchone()
        if row is None:
            raise BudgetConfigurationError(
                "BUDGET_CONFIGURATION_MISSING",
                "SQLite ledger configuration row is missing",
            )
        return row

    @staticmethod
    def _call(
        connection: sqlite3.Connection,
        logical_call_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM research_budget_calls
            WHERE logical_call_key = ?
            """,
            (logical_call_key,),
        ).fetchone()

    @classmethod
    def _required_call(
        cls,
        connection: sqlite3.Connection,
        logical_call_key: str,
    ) -> sqlite3.Row:
        row = cls._call(connection, logical_call_key)
        if row is None:
            raise CallNotFound(
                "LOGICAL_CALL_NOT_FOUND",
                f"Logical call {logical_call_key!r} does not exist",
            )
        return row

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        logical_call_key: str | None,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        details: Mapping[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO research_budget_audit (
                logical_call_key,
                event_type,
                from_status,
                to_status,
                details_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                logical_call_key,
                event_type,
                from_status,
                to_status,
                json.dumps(
                    details,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                _timestamp(),
            ),
        )


def _validated_logical_call_key(value: str) -> str:
    if not isinstance(value, str) or _LOGICAL_KEY_PATTERN.fullmatch(value) is None:
        raise LogicalCallConflict(
            "INVALID_LOGICAL_CALL_KEY",
            "logical_call_key must be a stable 1-256 character ASCII identity",
        )
    return value


def _validated_metadata_json(
    metadata: Mapping[str, MetadataValue] | None,
) -> str:
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise MetadataError("INVALID_METADATA", "metadata must be a mapping")
    unknown = set(metadata) - METADATA_IDENTITY_KEYS
    if unknown:
        raise MetadataError(
            "NON_IDENTITY_METADATA",
            f"metadata contains non-identity keys: {sorted(unknown)}",
        )
    normalized: dict[str, MetadataValue] = {}
    for key, value in metadata.items():
        if not isinstance(key, str):
            raise MetadataError("INVALID_METADATA_KEY", "metadata keys must be strings")
        if not isinstance(value, (str, int, bool)) and value is not None:
            raise MetadataError(
                "INVALID_METADATA_VALUE",
                f"metadata value for {key!r} must be a scalar identity",
            )
        if isinstance(value, str) and (len(value) > 256 or "\n" in value or "\r" in value):
            raise MetadataError(
                "INVALID_METADATA_VALUE",
                f"metadata value for {key!r} must be a single-line identity",
            )
        normalized[key] = value
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validated_reason(reason: str) -> str:
    if not isinstance(reason, str) or _REASON_PATTERN.fullmatch(reason) is None:
        raise ResearchBudgetError(
            "INVALID_UNKNOWN_REASON",
            "reason must be a short machine-readable error code",
        )
    return reason


def _validated_decimal(name: str, value: Decimal, *, positive: bool) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"{name} must be Decimal or int")
    decimal_value = Decimal(value)
    if not decimal_value.is_finite() or decimal_value < 0 or (positive and decimal_value == 0):
        raise ValueError(f"{name} must be a positive finite Decimal")
    return decimal_value


def _conservative_reservation_cny(
    input_price_per_million_cny: Decimal,
    output_price_per_million_cny: Decimal,
) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        input_cost = (
            Decimal(ALIYUN_DEEPSEEK_MAX_INPUT_TOKENS)
            * input_price_per_million_cny
            / _MILLION_TOKENS
        ).quantize(_CURRENCY_QUANTUM, rounding=ROUND_CEILING)
        output_cost = (
            Decimal(ALIYUN_DEEPSEEK_MAX_OUTPUT_TOKENS)
            * output_price_per_million_cny
            / _MILLION_TOKENS
        ).quantize(_CURRENCY_QUANTUM, rounding=ROUND_CEILING)
        return input_cost + output_cost


def _usage_validation_code(input_tokens: object, output_tokens: object) -> str | None:
    if type(input_tokens) is not int or type(output_tokens) is not int:
        return "USAGE_MUST_BE_INTEGER"
    if input_tokens < 0 or output_tokens < 0:
        return "USAGE_MUST_BE_NONNEGATIVE"
    if input_tokens > ALIYUN_DEEPSEEK_MAX_INPUT_TOKENS:
        return "INPUT_TOKENS_EXCEED_PROVIDER_UPPER"
    if output_tokens > ALIYUN_DEEPSEEK_MAX_OUTPUT_TOKENS:
        return "OUTPUT_TOKENS_EXCEED_PROVIDER_UPPER"
    return None


def _occupied_cny(connection: sqlite3.Connection) -> Decimal:
    occupied = Decimal(0)
    for row in connection.execute(
        """
        SELECT status, reserved_cny, actual_cny
        FROM research_budget_calls
        """
    ):
        if row["status"] == SETTLED:
            occupied += Decimal(row["actual_cny"])
        else:
            occupied += Decimal(row["reserved_cny"])
    return occupied


def _reservation(
    row: sqlite3.Row,
    *,
    newly_reserved: bool = False,
) -> Reservation:
    metadata = json.loads(row["metadata_json"])
    return Reservation(
        logical_call_key=row["logical_call_key"],
        status=row["status"],
        reserved_cny=Decimal(row["reserved_cny"]),
        actual_cny=Decimal(row["actual_cny"]) if row["actual_cny"] is not None else None,
        released_cny=(
            Decimal(row["released_cny"]) if row["released_cny"] is not None else Decimal(0)
        ),
        metadata=MappingProxyType(metadata),
        newly_reserved=newly_reserved,
    )


def _money_text(value: Decimal) -> str:
    return format(value.quantize(_CURRENCY_QUANTUM), "f")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
