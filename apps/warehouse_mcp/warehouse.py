"""The demo warehouse: schema, deterministic seed, metric dictionary, queries.

SQLite stands in for the company warehouse here. What the tools promise an
agent is the schema and the read-only guarantee, not the engine underneath, so
the same server can later be pointed at a real warehouse -- Postgres, Snowflake,
a query gateway -- without a single change to how the agent works. The part
that papering over does not cover is dialect: ``strftime`` here is
``date_trunc`` there, and a query an agent wrote against this demo will need
editing before it runs against the real thing. That is a real cost and it is
not hidden; what is bought with it is a server that runs offline, seeds in
milliseconds and tells the same story on every machine.

The seed is deterministic by construction. There is no randomness and no call
to ``date.today()``: every row is a function of the calendar window below, so a
test can assert on the exact conversion rates the data produces.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

from apps.warehouse_mcp.sql_guard import (
    MAX_ROW_CAP,
    STATEMENT_TIMEOUT_SECONDS,
    SqlGuardError,
    connect_read_only,
    ensure_read_only,
    statement_timeout,
)

# The window the demo covers. Fixed dates rather than an offset from today, so
# the seeded story does not drift out from under a saved query or a test.
WINDOW_START = date(2026, 2, 15)
WINDOW_END = date(2026, 3, 15)
ROLLOUT_START = date(2026, 3, 1)

COUNTRIES = ("US", "DE", "JP", "BR")
ORDER_AMOUNTS_CENTS = (1990, 4900, 12900, 2500, 7900)

# (platform, app_version, page views that day, share of them that sign up).
# The conversion rates are the only editorial content in the seed; everything
# else is mechanics. They are chosen so that the story is discoverable by
# grouping, and only by grouping -- no single aggregate gives it away.
_COHORTS_BEFORE_ROLLOUT = (
    ("android", "5.1.0", 20, 0.30),
    ("android", "5.1.1", 30, 0.30),
    ("ios", "5.1.0", 18, 0.31),
    ("ios", "5.1.1", 27, 0.31),
)
_COHORTS_AFTER_ROLLOUT = (
    ("android", "5.1.1", 20, 0.30),
    ("android", "5.2.0", 32, 0.12),
    ("ios", "5.1.1", 18, 0.31),
    ("ios", "5.2.0", 27, 0.31),
)

SCHEMA_SQL = """
CREATE TABLE signup_page_views (
    view_id     INTEGER PRIMARY KEY,
    viewed_at   TEXT    NOT NULL,
    platform    TEXT    NOT NULL,
    app_version TEXT    NOT NULL,
    country     TEXT    NOT NULL
);
CREATE TABLE signups (
    user_id      INTEGER PRIMARY KEY,
    signed_up_at TEXT    NOT NULL,
    platform     TEXT    NOT NULL,
    app_version  TEXT    NOT NULL,
    country      TEXT    NOT NULL
);
CREATE TABLE orders (
    order_id     INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    ordered_at   TEXT    NOT NULL,
    amount_cents INTEGER NOT NULL,
    status       TEXT    NOT NULL
);
CREATE TABLE payments (
    payment_id   INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL,
    paid_at      TEXT    NOT NULL,
    amount_cents INTEGER NOT NULL,
    status       TEXT    NOT NULL
);
CREATE INDEX idx_views_day ON signup_page_views (viewed_at);
CREATE INDEX idx_signups_day ON signups (signed_up_at);
CREATE INDEX idx_orders_user ON orders (user_id);
CREATE INDEX idx_payments_order ON payments (order_id);
"""

TABLE_NAMES = ("signup_page_views", "signups", "orders", "payments")

TABLES: dict[str, dict[str, Any]] = {
    "signup_page_views": {
        "description": (
            "One row per view of the registration page. The denominator of "
            "registration conversion; not deduplicated by device or session."
        ),
        "columns": (
            ("view_id", "INTEGER", False, "Surrogate key."),
            ("viewed_at", "TEXT", False, "UTC timestamp, ISO-8601 'YYYY-MM-DDTHH:MM:SSZ'."),
            ("platform", "TEXT", False, "'android' or 'ios'."),
            ("app_version", "TEXT", False, "Client build that rendered the page, e.g. '5.2.0'."),
            ("country", "TEXT", False, "Two-letter country of the viewing device."),
        ),
    },
    "signups": {
        "description": (
            "One row per completed registration. Joins to orders on user_id. "
            "Carries the platform and app version the registration completed on."
        ),
        "columns": (
            ("user_id", "INTEGER", False, "Surrogate key; the user identifier orders refer to."),
            ("signed_up_at", "TEXT", False, "UTC timestamp the registration completed."),
            ("platform", "TEXT", False, "'android' or 'ios'."),
            ("app_version", "TEXT", False, "Client build the registration completed on."),
            ("country", "TEXT", False, "Two-letter country at registration time."),
        ),
    },
    "orders": {
        "description": (
            "One row per order. status is PAID, REFUNDED or CANCELLED; a CANCELLED "
            "order never reached payment."
        ),
        "columns": (
            ("order_id", "INTEGER", False, "Surrogate key."),
            ("user_id", "INTEGER", False, "The signups.user_id that placed the order."),
            ("ordered_at", "TEXT", False, "UTC timestamp the order was placed."),
            ("amount_cents", "INTEGER", False, "Order total in minor units; never negative."),
            ("status", "TEXT", False, "PAID | REFUNDED | CANCELLED."),
        ),
    },
    "payments": {
        "description": (
            "One row per payment attempt, so an order may have several. status is "
            "SUCCEEDED, FAILED or REFUNDED."
        ),
        "columns": (
            ("payment_id", "INTEGER", False, "Surrogate key."),
            ("order_id", "INTEGER", False, "The orders.order_id being paid."),
            ("paid_at", "TEXT", False, "UTC timestamp of the attempt, successful or not."),
            ("amount_cents", "INTEGER", False, "Attempted amount in minor units."),
            ("status", "TEXT", False, "SUCCEEDED | FAILED | REFUNDED."),
        ),
    },
}

# The metric dictionary is a set of business decisions, not derivations. Each
# one is written the way a data team writes them down: the formula, and the
# caveat that makes the choice arguable, because the argument is what the agent
# has to surface to a human rather than quietly settle on its own.
METRICS: dict[str, dict[str, Any]] = {
    "registration_conversion_rate": {
        "display_name": "Registration conversion rate",
        "definition": (
            "Completed registrations divided by registration page views in the same "
            "reporting window. The tables do not link a view to a registration, so "
            "their count difference cannot identify unsuccessful views or visitors."
        ),
        "formula": "COUNT(signups) / COUNT(signup_page_views)",
        "numerator": "Completed registrations in signups.",
        "denominator": (
            "Rows in signup_page_views, NOT deduplicated: a user who opens the "
            "registration page three times contributes three views."
        ),
        "caveats": (
            "The denominator is raw views, so anything that makes users retry the "
            "page -- a crash, a slow form, a validation loop -- lowers this metric "
            "without a single user changing their mind. It is also not a cohort "
            "measure: a view shortly before the window closes has less time to "
            "result in an in-window registration. Same-day registration is possible. "
            "Deduplicating by device was "
            "considered and rejected because signup_page_views carries no stable "
            "device key."
        ),
        "source_tables": ["signup_page_views", "signups"],
    },
    "paying_user": {
        "display_name": "Paying user",
        "definition": (
            "A registered user whose first payment attempt succeeded. Orders that "
            "were later refunded do not count, and neither do cancelled orders."
        ),
        "formula": (
            "Rank payment attempts per orders.user_id by payments.paid_at, payments.payment_id "
            "across their order history. Count distinct registered users whose first attempt "
            "has payments.status = 'SUCCEEDED' and whose corresponding orders.status = 'PAID'. "
            "Apply status filters after selecting the first attempt."
        ),
        "numerator": "Distinct signups.user_id meeting the condition.",
        "denominator": None,
        "caveats": (
            "'First payment succeeded' excludes a user whose first attempt failed "
            "and whose retry went through -- commercially a customer, definitionally "
            "not one here. Finance owns that line and has kept it deliberately "
            "strict. A nonqualifying first attempt can also have other statuses: "
            "use the returned first-attempt breakdown when attributing the difference "
            "from 'any successful payment'."
        ),
        "source_tables": ["signups", "orders", "payments"],
    },
    "active_user": {
        "display_name": "Active user",
        "definition": (
            "A registered user who placed at least one order in the window, "
            "whatever became of that order."
        ),
        "formula": "COUNT(DISTINCT orders.user_id) over the window",
        "numerator": "Distinct users with one or more orders.",
        "denominator": None,
        "caveats": (
            "Orders establish activity under this definition. signup_page_views counts visits "
            "to the registration page; it cannot identify everyone who opened the app. "
            "There is no general app-open or session source. Cancelled orders still "
            "count as activity: the intent was real even though the revenue was not."
        ),
        "source_tables": ["signups", "orders"],
    },
    "arpu": {
        "display_name": "ARPU (average revenue per user)",
        "definition": (
            "Net revenue in the window divided by the number of registered users "
            "in the window. Refunds are subtracted from revenue; cancelled orders "
            "never enter it."
        ),
        "formula": (
            "SUM(orders.amount_cents WHERE status = 'PAID') / COUNT(DISTINCT signups.user_id)"
        ),
        "numerator": "Paid order value in cents, with refunded orders excluded.",
        "denominator": "All registered users in the window, not only paying ones.",
        "caveats": (
            "The denominator is every registered user, so ARPU falls when "
            "registration grows faster than spend -- which reads as a problem and is "
            "often the opposite. A refund is subtracted in the window it belongs to "
            "here, not the window the original order fell in, which makes "
            "month-over-month comparisons slightly unfair to the later month."
        ),
        "source_tables": ["signups", "orders"],
    },
}


class WarehouseError(Exception):
    """A tool could not be answered from the warehouse.

    The message is written to be read by an agent: it says what was wrong and,
    where a closed set exists, what the valid options are.
    """


def _utc_date_window(window: Any) -> tuple[str, str]:
    if not isinstance(window, dict) or set(window) != {"start_date", "end_date"}:
        raise WarehouseError("Each window needs start_date and end_date.")
    start, end = window["start_date"], window["end_date"]
    try:
        if any(date.fromisoformat(v).isoformat() != v for v in (start, end)):
            raise ValueError
    except (TypeError, ValueError):
        raise WarehouseError("Use valid UTC dates in YYYY-MM-DD form.") from None
    if start > end:
        raise WarehouseError("Date windows must be chronological and non-overlapping.")
    return start, end


class Warehouse:
    """Owns the demo database file and every read taken from it.

    Construction is cheap; :meth:`build` does the work and is called once at
    startup. The tables are dropped and rebuilt each time rather than reused,
    because a file left over from an older seed would answer questions with
    numbers no test agrees with.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)

    @property
    def path(self) -> Path:
        return self._path

    def build(self) -> None:
        """Create the schema and write the deterministic seed."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        try:
            with connection:
                for table in TABLE_NAMES:
                    connection.execute(f"DROP TABLE IF EXISTS {table}")
                connection.executescript(SCHEMA_SQL)
                views, signups, orders, payments = build_seed()
                connection.executemany(
                    "INSERT INTO signup_page_views VALUES (?, ?, ?, ?, ?)", views
                )
                connection.executemany("INSERT INTO signups VALUES (?, ?, ?, ?, ?)", signups)
                connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders)
                connection.executemany("INSERT INTO payments VALUES (?, ?, ?, ?, ?)", payments)
        finally:
            connection.close()

    def list_tables(self) -> dict[str, Any]:
        connection = connect_read_only(self._path)
        try:
            return {
                "tables": [
                    {
                        "name": name,
                        "row_count": _count_rows(connection, name),
                        "description": TABLES[name]["description"],
                    }
                    for name in TABLE_NAMES
                ]
            }
        finally:
            connection.close()

    def describe_table(self, table: str) -> dict[str, Any]:
        if table not in TABLES:
            raise WarehouseError(
                f"Unknown table: {table!r}. This warehouse has: {', '.join(TABLE_NAMES)}."
            )
        meta = TABLES[table]
        connection = connect_read_only(self._path)
        try:
            row_count = _count_rows(connection, table)
            sample_values = _sample_values(connection, table, meta["columns"])
        finally:
            connection.close()
        return {
            "table": table,
            "description": meta["description"],
            "columns": [
                {"name": name, "type": type_, "nullable": nullable, "description": description}
                for name, type_, nullable, description in meta["columns"]
            ],
            "row_count": row_count,
            "sample_values": sample_values,
        }

    def registration_conversion(
        self, windows: Any, platform: str | None = None, breakdown: str = "none"
    ) -> dict[str, Any]:
        """Count each event once, including segments with only one kind of event."""
        if not isinstance(windows, list) or not 1 <= len(windows) <= 2:
            raise WarehouseError("Supply one or two chronological UTC date windows.")
        bounds: list[tuple[str, str]] = []
        for window in windows:
            start, end = _utc_date_window(window)
            if bounds and bounds[-1][1] >= start:
                raise WarehouseError("Date windows must be chronological and non-overlapping.")
            bounds.append((start, end))
        if platform not in (None, "android", "ios") or breakdown not in (
            "none",
            "platform",
            "platform_version",
        ):
            raise WarehouseError("Unknown platform or breakdown.")

        # SQLite date() normalizes offset-bearing timestamps to UTC. Aggregate
        # the two streams separately: a signup must never multiply page views.
        start1, end1 = bounds[0]
        start2, end2 = bounds[1] if len(bounds) == 2 else (None, None)
        sql = """
            WITH windows(period, first_day, last_day) AS (VALUES (0, ?, ?), (1, ?, ?)),
            counts AS (
                SELECT period, platform, app_version, count(*) AS signups, 0 AS views
                FROM windows JOIN signups ON date(signed_up_at) BETWEEN first_day AND last_day
                WHERE (? IS NULL OR platform = ?) GROUP BY period, platform, app_version
                UNION ALL
                SELECT period, platform, app_version, 0, count(*)
                FROM windows JOIN signup_page_views
                    ON date(viewed_at) BETWEEN first_day AND last_day
                WHERE (? IS NULL OR platform = ?) GROUP BY period, platform, app_version
            )
            SELECT period, platform, app_version, sum(signups), sum(views)
            FROM counts GROUP BY period, platform, app_version
            ORDER BY platform, app_version, period
        """

        def empty_counts() -> list[list[int]]:
            return [[0, 0] for _ in bounds]

        totals = empty_counts()
        platforms: dict[str, list[list[int]]] = {}
        versions: dict[tuple[str, str], list[list[int]]] = {}
        connection = connect_read_only(self._path)
        try:
            with statement_timeout(connection):
                for period, plat, version, signups, views in connection.execute(
                    sql, (start1, end1, start2, end2, platform, platform, platform, platform)
                ):
                    groups = [totals]
                    if breakdown != "none":
                        groups.append(platforms.setdefault(plat, empty_counts()))
                    if breakdown == "platform_version":
                        groups.append(versions.setdefault((plat, version), empty_counts()))
                    if 1 + len(platforms) + len(versions) > MAX_ROW_CAP:
                        raise WarehouseError(
                            f"Conversion breakdown exceeds {MAX_ROW_CAP} result groups. "
                            "Narrow the dates, filter by platform, or request a coarser breakdown."
                        )
                    for group in groups:
                        group[period][0] += signups
                        group[period][1] += views
        except sqlite3.DatabaseError as exc:
            raise WarehouseError(f"The warehouse rejected the conversion query: {exc}") from None
        finally:
            connection.close()

        def summary(counts: list[list[int]]) -> dict[str, Any]:
            rates = [Fraction(100 * n, d) if d else None for n, d in counts]
            change = None
            if len(rates) == 2 and rates[0] is not None and rates[1] is not None:
                change = float(rates[1] - rates[0])
            return {
                "periods": [
                    {"signups": n, "views": d, "rate_pct": float(r) if r is not None else None}
                    for (n, d), r in zip(counts, rates, strict=True)
                ],
                "change_pp": change,
            }

        return {
            "windows": windows,
            "overall": summary(totals),
            "platforms": [{"platform": p, **summary(cnt)} for p, cnt in platforms.items()],
            "versions": [
                {"platform": p, "app_version": v, **summary(cnt)}
                for (p, v), cnt in versions.items()
            ],
        }

    def order_activity(self, window: dict[str, Any]) -> dict[str, Any]:
        """Group people by whether any of their orders in this UTC window was PAID."""
        start, end = _utc_date_window(window)
        sql = """
            WITH per_user AS (
                SELECT user_id, count(*) AS n, sum(status = 'PAID') AS paid,
                       sum(status = 'REFUNDED') AS refunded, sum(status = 'CANCELLED') AS cancelled
                FROM orders WHERE date(ordered_at) BETWEEN ? AND ?
                GROUP BY user_id
            )
            SELECT paid > 0 AS has_paid, count(*), sum(n), sum(paid), sum(refunded), sum(cancelled)
            FROM per_user GROUP BY has_paid
        """
        fields = (
            "users",
            "order_rows",
            "paid_rows",
            "non_paid_rows",
            "refunded_rows",
            "cancelled_rows",
            "other_status_rows",
        )
        groups = {
            key: dict.fromkeys(fields, 0) for key in ("with_paid_order", "without_paid_order")
        }
        connection = connect_read_only(self._path)
        try:
            with statement_timeout(connection):
                for has_paid, users, n, paid, refunded, cancelled in connection.execute(
                    sql, (start, end)
                ):
                    groups["with_paid_order" if has_paid else "without_paid_order"] = dict(
                        users=users,
                        order_rows=n,
                        paid_rows=paid,
                        non_paid_rows=n - paid,
                        refunded_rows=refunded,
                        cancelled_rows=cancelled,
                        other_status_rows=n - paid - refunded - cancelled,
                    )
        except sqlite3.DatabaseError as exc:
            raise WarehouseError(
                f"The warehouse rejected the order activity query: {exc}"
            ) from None
        finally:
            connection.close()
        return {
            "window": window,
            "ordered_users": sum(g["users"] for g in groups.values()),
            **groups,
        }

    def paying_user_counts(self) -> dict[str, Any]:
        """Compare registered users' first attempt with any attempt across all history."""
        # Rank the complete history before applying either status condition.
        # Once reduced to the first row, each user can enter only one difference bucket.
        sql = """
            WITH history AS (
                SELECT o.user_id, p.paid_at, p.payment_id, p.status AS first_status,
                       (p.status = 'SUCCEEDED' AND o.status = 'PAID') AS qualified
                FROM signups s
                JOIN orders o ON o.user_id = s.user_id
                JOIN payments p ON p.order_id = o.order_id
            ), ranked AS (
                SELECT first_status, qualified,
                       row_number() OVER (
                           PARTITION BY user_id ORDER BY paid_at, payment_id
                       ) AS attempt_number,
                       max(qualified) OVER (PARTITION BY user_id) AS any_qualified
                FROM history
            )
            SELECT count(*) FILTER (WHERE qualified),
                   count(*),
                   count(*) FILTER (WHERE NOT qualified AND first_status = 'FAILED'),
                   count(*) FILTER (WHERE NOT qualified AND first_status = 'REFUNDED'),
                   count(*) FILTER (
                       WHERE NOT qualified AND first_status NOT IN ('FAILED', 'REFUNDED')
                   )
            FROM ranked WHERE attempt_number = 1 AND any_qualified
        """
        first, any_attempt, failed, refunded, other = self.query(sql, 1)["rows"][0]
        return {
            "strict_first_qualified_users": first,
            "any_qualified_users": any_attempt,
            "any_only_by_first_payment_status": {
                "failed": failed,
                "refunded": refunded,
                "other": other,
            },
        }

    def query(self, sql: str, limit: int) -> dict[str, Any]:
        """Run one guarded SELECT and return JSON-safe rows.

        ``limit + 1`` rows are fetched so truncation can be reported as a fact
        rather than inferred from a full page, which would also be true of a
        result that happened to be exactly ``limit`` rows long.
        """

        statement = ensure_read_only(sql)
        capped = max(1, min(int(limit), MAX_ROW_CAP))
        started = time.perf_counter()
        connection = connect_read_only(self._path)
        try:
            with statement_timeout(connection):
                cursor = connection.execute(statement)
                columns = [str(column[0]) for column in (cursor.description or ())]
                fetched = cursor.fetchmany(capped + 1)
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise SqlGuardError(
                    "The query was stopped after "
                    f"{STATEMENT_TIMEOUT_SECONDS:g} seconds. Narrow it and try again."
                ) from None
            # SQLite's own message names the missing table or column, which is
            # exactly what the agent needs to fix its query.
            raise WarehouseError(f"The warehouse rejected the query: {exc}") from None
        except sqlite3.DatabaseError as exc:
            raise WarehouseError(f"The warehouse rejected the query: {exc}") from None
        finally:
            connection.close()

        truncated = len(fetched) > capped
        rows = [[_json_safe(value) for value in row] for row in fetched[:capped]]
        return {
            "sql": statement,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }


def metric_definition(metric: str) -> dict[str, Any]:
    """Look one metric up in the dictionary, or say which ones exist."""

    key = metric.strip().lower() if isinstance(metric, str) else ""
    entry = METRICS.get(key)
    if entry is None:
        raise WarehouseError(
            f"Unknown metric: {metric!r}. Defined metrics are: {', '.join(sorted(METRICS))}."
        )
    return {"metric": key, **entry, "source_tables": list(entry["source_tables"])}


def build_seed() -> tuple[list[tuple], list[tuple], list[tuple], list[tuple]]:
    """Produce every row the demo warehouse holds, in insertion order.

    Kept a pure function so a test can reason about the numbers without a file
    on disk, and so the story the data tells is reviewable in one place.
    """

    views: list[tuple] = []
    signups: list[tuple] = []
    view_id = 0
    user_id = 0

    for day in _days():
        cohorts = _COHORTS_BEFORE_ROLLOUT if day < ROLLOUT_START else _COHORTS_AFTER_ROLLOUT
        for platform, app_version, view_count, rate in cohorts:
            converted = round(view_count * rate)
            for index in range(view_count):
                view_id += 1
                country = COUNTRIES[view_id % len(COUNTRIES)]
                viewed_at = _timestamp(day, index, 8)
                views.append((view_id, viewed_at, platform, app_version, country))
                if not _converts(index, view_count, converted):
                    continue
                user_id += 1
                signups.append(
                    (
                        user_id,
                        _timestamp(day, index, 8, offset_minutes=4),
                        platform,
                        app_version,
                        country,
                    )
                )

    orders, payments = _build_orders_and_payments(signups)
    return views, signups, orders, payments


def _build_orders_and_payments(
    signups: list[tuple],
) -> tuple[list[tuple], list[tuple]]:
    """Give some registered users orders, and most orders a payment.

    Purchase behaviour is deliberately independent of platform and version: the
    only signal planted in this warehouse is in registration conversion, so an
    analyst who chases revenue instead finds nothing, which is the honest shape
    of the exercise.
    """

    orders: list[tuple] = []
    payments: list[tuple] = []
    order_id = 0
    payment_id = 0

    for index, (user, signed_up_at, _platform, _version, _country) in enumerate(signups):
        order_count = (1 if index % 2 == 0 else 0) + (1 if index % 5 == 0 else 0)
        for repeat in range(order_count):
            order_id += 1
            if order_id % 17 == 0:
                status = "CANCELLED"
            elif order_id % 11 == 0:
                status = "REFUNDED"
            else:
                status = "PAID"
            amount = ORDER_AMOUNTS_CENTS[order_id % len(ORDER_AMOUNTS_CENTS)]
            ordered_at = _shift_days(signed_up_at, repeat + 1)
            orders.append((order_id, user, ordered_at, amount, status))
            if status == "CANCELLED":
                continue
            if order_id % 13 == 0:
                # A failed first attempt followed by a successful retry: the
                # case that makes the paying_user definition arguable, present
                # in the data so the argument is not hypothetical.
                payment_id += 1
                payments.append((payment_id, order_id, ordered_at, amount, "FAILED"))
            payment_id += 1
            payments.append(
                (
                    payment_id,
                    order_id,
                    ordered_at,
                    amount,
                    "REFUNDED" if status == "REFUNDED" else "SUCCEEDED",
                )
            )
    return orders, payments


def _converts(index: int, total: int, converted: int) -> bool:
    """Spread ``converted`` signups evenly across ``total`` views.

    Taking the first N views instead would tie conversion to whatever else is
    ordered with the index -- country, here -- and invent a correlation the
    story never intended.
    """

    if converted <= 0:
        return False
    return (index * converted) % total < converted


def _days() -> list[date]:
    span = (WINDOW_END - WINDOW_START).days
    return [WINDOW_START + timedelta(days=offset) for offset in range(span + 1)]


def _timestamp(day: date, index: int, first_hour: int, *, offset_minutes: int = 0) -> str:
    """A stable UTC timestamp inside the working part of the day."""

    minute_of_day = (index * 7 + offset_minutes) % (11 * 60)
    hour = first_hour + minute_of_day // 60
    return f"{day.isoformat()}T{hour:02d}:{minute_of_day % 60:02d}:{(index * 13) % 60:02d}Z"


def _shift_days(timestamp: str, days: int) -> str:
    day = date.fromisoformat(timestamp[:10]) + timedelta(days=days)
    return f"{day.isoformat()}{timestamp[10:]}"


def _count_rows(connection: sqlite3.Connection, table: str) -> int:
    # The table name is interpolated because SQLite will not parameterize an
    # identifier; it is safe here only because it comes from TABLE_NAMES, never
    # from the caller.
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _sample_values(
    connection: sqlite3.Connection, table: str, columns: tuple[tuple[Any, ...], ...]
) -> dict[str, list[Any]] | None:
    """A few distinct values per column, to save a round trip of guessing.

    Only low-cardinality columns are sampled: listing five of 2785 view ids
    would tell an analyst nothing, whereas the three order statuses tell them
    what they may filter on.
    """

    samples: dict[str, list[Any]] = {}
    for column in columns:
        name = str(column[0])
        rows = connection.execute(
            f"SELECT DISTINCT {name} FROM {table} ORDER BY {name} LIMIT 6"
        ).fetchall()
        values = [_json_safe(row[0]) for row in rows]
        if len(values) <= 5:
            samples[name] = values
    return samples or None


def _json_safe(value: Any) -> Any:
    """Reduce a SQLite value to null, a number or a string.

    The output schema promises scalars a JSON encoder can carry. BLOBs and
    anything else the engine hands back become their string form rather than
    breaking the response.
    """

    if value is None or isinstance(value, int | float | str):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


__all__ = [
    "COUNTRIES",
    "METRICS",
    "ROLLOUT_START",
    "SCHEMA_SQL",
    "TABLES",
    "TABLE_NAMES",
    "WINDOW_END",
    "WINDOW_START",
    "Warehouse",
    "WarehouseError",
    "build_seed",
    "metric_definition",
]
