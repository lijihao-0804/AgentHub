"""Unit tests for the Warehouse MCP Server.

Nothing here touches the network; the warehouse is seeded into a temp directory
per test session. Two things are worth stating about what is asserted below.

The SQL guard is tested by what it refuses, one statement kind at a time, plus
the three evasions that make a keyword grep insufficient: a second statement
after a semicolon, a separator hidden inside a comment, and a semicolon that is
merely part of a string literal and must therefore be allowed.

The conversion test asserts the exact numbers the seed produces. The point of
the seeded story is that it is discoverable by querying, so the test discovers
it the same way an analyst would -- by grouping page views and signups -- rather
than by reading a constant out of the seed module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import mcp_types
import pytest

from apps.warehouse_mcp import server
from apps.warehouse_mcp.server import (
    DESCRIBE_TABLE,
    GET_METRIC_DEFINITION,
    LIST_TABLES,
    QUERY_SQL,
)
from apps.warehouse_mcp.sql_guard import (
    SqlGuardError,
    connect_read_only,
    ensure_read_only,
    split_statements,
    strip_sql_comments,
)
from apps.warehouse_mcp.warehouse import METRICS, SCHEMA_SQL, TABLE_NAMES, Warehouse

EXPECTED_TOOLS = (
    GET_METRIC_DEFINITION,
    LIST_TABLES,
    DESCRIBE_TABLE,
    QUERY_SQL,
    server.GET_REGISTRATION_CONVERSION,
    server.GET_ORDER_ACTIVITY,
    server.GET_PAYING_USER_COUNTS,
)

# Grouped conversion over the seeded window, split at the 5.2.0 rollout. This
# is the query an analyst would arrive at, and the numbers below are whatever
# the seed actually produces for it.
CONVERSION_SQL = """
WITH views AS (
    SELECT platform, app_version,
           CASE WHEN substr(viewed_at, 1, 10) < '2026-03-01' THEN 'before' ELSE 'after' END
               AS period,
           COUNT(*) AS view_count
    FROM signup_page_views
    GROUP BY 1, 2, 3
),
registrations AS (
    SELECT platform, app_version,
           CASE WHEN substr(signed_up_at, 1, 10) < '2026-03-01' THEN 'before' ELSE 'after' END
               AS period,
           COUNT(*) AS signup_count
    FROM signups
    GROUP BY 1, 2, 3
)
SELECT v.platform, v.app_version, v.period, v.view_count,
       COALESCE(r.signup_count, 0) AS signup_count
FROM views v
LEFT JOIN registrations r
  ON r.platform = v.platform AND r.app_version = v.app_version AND r.period = v.period
ORDER BY 1, 2, 3
"""


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Warehouse:
    built = Warehouse(tmp_path_factory.mktemp("warehouse") / "warehouse.sqlite3")
    built.build()
    return built


async def call_tool(
    warehouse: Warehouse, name: str, arguments: dict[str, Any] | None = None
) -> mcp_types.CallToolResult:
    """Drive one tool through the registered low-level handler.

    Going through the handler rather than the private helpers is what keeps the
    error path honest: an exception that escaped would fail here, not be
    swallowed into an isError result somewhere the test never looked.
    """

    entry = server.build_server(warehouse).get_request_handler("tools/call")
    assert entry is not None
    params = mcp_types.CallToolRequestParams(name=name, arguments=arguments or {})
    return await entry.handler(None, params)  # type: ignore[arg-type]


def text_of(result: mcp_types.CallToolResult) -> str:
    return "\n".join(
        block.text for block in result.content if isinstance(block, mcp_types.TextContent)
    )


# -- the tool surface -------------------------------------------------------


@pytest.mark.asyncio
async def test_original_read_tools_and_optional_aggregates_are_listed(warehouse: Warehouse) -> None:
    entry = server.build_server(warehouse).get_request_handler("tools/list")
    assert entry is not None
    listed = await entry.handler(None, None)  # type: ignore[arg-type]
    assert tuple(tool.name for tool in listed.tools) == EXPECTED_TOOLS


@pytest.mark.parametrize("tool", server.TOOLS, ids=[tool.name for tool in server.TOOLS])
def test_every_tool_is_annotated_read_only(tool: mcp_types.Tool) -> None:
    annotations = tool.annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True
    assert annotations.open_world_hint is False


@pytest.mark.parametrize("tool", server.TOOLS, ids=[tool.name for tool in server.TOOLS])
def test_every_schema_closes_the_door_on_extra_properties(tool: mcp_types.Tool) -> None:
    assert tool.input_schema["additionalProperties"] is False
    assert tool.output_schema is not None
    assert tool.output_schema["additionalProperties"] is False


@pytest.mark.parametrize("tool", server.TOOLS, ids=[tool.name for tool in server.TOOLS])
def test_every_output_field_is_required(tool: mcp_types.Tool) -> None:
    schema = tool.output_schema
    assert schema is not None
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.asyncio
async def test_unknown_arguments_are_refused_not_dropped(warehouse: Warehouse) -> None:
    # Dropping `country` silently would tell the caller it filtered by country.
    result = await call_tool(warehouse, LIST_TABLES, {"country": "US"})
    assert result.is_error is True
    assert "country" in text_of(result)


# -- the SQL guard ----------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO signups VALUES (1, '2026-03-01', 'android', '5.2.0', 'US')",
        "UPDATE signups SET country = 'US'",
        "DELETE FROM signups",
        "DROP TABLE signups",
        "ALTER TABLE signups ADD COLUMN x TEXT",
        "TRUNCATE TABLE signups",
        "CREATE TABLE t (a INTEGER)",
        "REPLACE INTO signups VALUES (1, '', '', '', '')",
        "ATTACH DATABASE 'other.db' AS other",
        "DETACH DATABASE other",
        "PRAGMA table_info(signups)",
        "VACUUM",
        "GRANT SELECT ON signups TO analyst",
        "BEGIN TRANSACTION",
        "EXPLAIN SELECT 1",
        "not sql at all",
    ],
)
def test_every_non_select_statement_kind_is_rejected(statement: str) -> None:
    with pytest.raises(SqlGuardError) as error:
        ensure_read_only(statement)
    # The message has to name the kind, or the caller cannot tell a rejected
    # DELETE from a syntax error it should retry differently.
    assert statement.split()[0].upper() in str(error.value).upper()


def test_a_write_wearing_a_with_prefix_is_still_rejected() -> None:
    with pytest.raises(SqlGuardError) as error:
        ensure_read_only("WITH doomed AS (SELECT user_id FROM signups) DELETE FROM signups")
    assert "DELETE" in str(error.value)


def test_a_second_statement_after_a_semicolon_is_rejected() -> None:
    with pytest.raises(SqlGuardError) as error:
        ensure_read_only("SELECT 1; DROP TABLE signups")
    assert "2 statements" in str(error.value)


def test_a_statement_hidden_behind_a_line_comment_is_rejected() -> None:
    # The separator lives inside the comment, so a splitter that ran before the
    # stripper would see one harmless statement.
    with pytest.raises(SqlGuardError):
        ensure_read_only("SELECT 1 -- ;\nDROP TABLE signups")


def test_a_statement_hidden_behind_a_block_comment_is_rejected() -> None:
    with pytest.raises(SqlGuardError):
        ensure_read_only("SELECT 1 /* ; */ ; DROP TABLE signups")


def test_a_keyword_hidden_behind_a_block_comment_cannot_reach_the_engine() -> None:
    with pytest.raises(SqlGuardError):
        ensure_read_only("SELECT 1 /* harmless */; /* also harmless */ DELETE FROM signups")


def test_comments_are_stripped_but_string_literals_are_not() -> None:
    assert strip_sql_comments("SELECT 1 -- trailing\nFROM t").split() == [
        "SELECT",
        "1",
        "FROM",
        "t",
    ]
    assert strip_sql_comments("SELECT '-- not a comment'") == "SELECT '-- not a comment'"
    assert strip_sql_comments("SELECT '/* nor this */'") == "SELECT '/* nor this */'"


def test_an_unterminated_block_comment_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(SqlGuardError):
        ensure_read_only("SELECT 1 /* never closed")


def test_a_semicolon_inside_a_string_literal_is_not_a_separator() -> None:
    statement = "SELECT * FROM signups WHERE country = 'US;DROP TABLE signups'"
    assert split_statements(statement) == [statement]
    assert ensure_read_only(statement) == statement


def test_a_forbidden_word_inside_a_string_literal_is_just_data() -> None:
    statement = "SELECT * FROM orders WHERE status = 'DELETE ME'"
    assert ensure_read_only(statement) == statement


def test_a_trailing_semicolon_is_allowed() -> None:
    assert ensure_read_only("SELECT 1;") == "SELECT 1"


def test_a_plain_select_is_accepted() -> None:
    assert ensure_read_only("  SELECT count(*) FROM signups  ") == "SELECT count(*) FROM signups"


def test_a_with_select_is_accepted() -> None:
    statement = "WITH d AS (SELECT 1 AS n) SELECT n FROM d"
    assert ensure_read_only(statement) == statement


def test_a_with_that_never_selects_is_rejected() -> None:
    with pytest.raises(SqlGuardError):
        ensure_read_only("WITH d AS (VALUES (1))")


def test_an_empty_statement_is_rejected() -> None:
    with pytest.raises(SqlGuardError):
        ensure_read_only("   ")


def test_the_read_only_connection_refuses_a_write_the_parser_never_saw(
    warehouse: Warehouse,
) -> None:
    # The guarantee, as distinct from the explanation: this write bypasses
    # ensure_read_only entirely and still cannot land.
    connection = connect_read_only(warehouse.path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM signups")
    finally:
        connection.close()


def test_the_guard_refuses_a_database_that_was_never_built(tmp_path: Path) -> None:
    with pytest.raises(SqlGuardError):
        connect_read_only(tmp_path / "absent.sqlite3")


# -- query_sql --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_select_returns_columns_and_json_safe_rows(warehouse: Warehouse) -> None:
    result = await call_tool(
        warehouse,
        QUERY_SQL,
        {"sql": "SELECT platform, COUNT(*) AS n FROM signups GROUP BY 1 ORDER BY 1"},
    )
    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    assert payload["columns"] == ["platform", "n"]
    assert [row[0] for row in payload["rows"]] == ["android", "ios"]
    assert payload["truncated"] is False
    assert payload["row_count"] == 2
    assert payload["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_the_limit_truncates_and_says_so(warehouse: Warehouse) -> None:
    result = await call_tool(
        warehouse, QUERY_SQL, {"sql": "SELECT view_id FROM signup_page_views", "limit": 5}
    )
    payload = result.structured_content
    assert payload is not None
    assert payload["row_count"] == 5
    assert payload["truncated"] is True


@pytest.mark.asyncio
async def test_a_result_that_fits_is_not_reported_as_truncated(warehouse: Warehouse) -> None:
    # Exactly `limit` rows is the case a naive implementation gets wrong.
    result = await call_tool(
        warehouse, QUERY_SQL, {"sql": "SELECT view_id FROM signup_page_views LIMIT 5", "limit": 5}
    )
    payload = result.structured_content
    assert payload is not None
    assert payload["row_count"] == 5
    assert payload["truncated"] is False


@pytest.mark.asyncio
async def test_a_rejected_statement_comes_back_as_a_tool_error(warehouse: Warehouse) -> None:
    result = await call_tool(warehouse, QUERY_SQL, {"sql": "DROP TABLE signups"})
    assert result.is_error is True
    assert "DROP" in text_of(result)


@pytest.mark.asyncio
async def test_a_query_against_a_missing_table_is_an_error_not_a_crash(
    warehouse: Warehouse,
) -> None:
    result = await call_tool(warehouse, QUERY_SQL, {"sql": "SELECT * FROM no_such_table"})
    assert result.is_error is True
    assert "no_such_table" in text_of(result)


# -- the metric dictionary and the catalogue --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("metric", sorted(METRICS))
async def test_every_defined_metric_answers_with_the_full_contract(
    warehouse: Warehouse, metric: str
) -> None:
    field = server.METRIC_INPUT_SCHEMA["properties"]["metric"]
    assert metric in field["description"].split(": ")[-1].split(", ")
    assert "enum" not in field
    result = await call_tool(warehouse, GET_METRIC_DEFINITION, {"metric": f" {metric.upper()} "})
    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    assert set(payload) == set(server.METRIC_OUTPUT_SCHEMA["properties"])
    assert payload["metric"] == metric
    assert payload["caveats"]
    assert payload["source_tables"]


@pytest.mark.asyncio
async def test_the_conversion_metric_names_its_denominator_explicitly(
    warehouse: Warehouse,
) -> None:
    result = await call_tool(
        warehouse, GET_METRIC_DEFINITION, {"metric": "registration_conversion_rate"}
    )
    payload = result.structured_content
    assert payload is not None
    assert "signup_page_views" in payload["denominator"]
    assert "NOT deduplicated" in payload["denominator"]


@pytest.mark.asyncio
async def test_an_unknown_metric_lists_the_metrics_that_do_exist(warehouse: Warehouse) -> None:
    result = await call_tool(warehouse, GET_METRIC_DEFINITION, {"metric": "churn_rate"})
    assert result.is_error is True
    message = text_of(result)
    for metric in METRICS:
        assert metric in message


@pytest.mark.asyncio
async def test_list_tables_reports_every_table_with_a_row_count(warehouse: Warehouse) -> None:
    result = await call_tool(warehouse, LIST_TABLES)
    payload = result.structured_content
    assert payload is not None
    assert tuple(table["name"] for table in payload["tables"]) == TABLE_NAMES
    assert all(table["row_count"] > 0 for table in payload["tables"])
    assert all(table["description"] for table in payload["tables"])


@pytest.mark.asyncio
async def test_describe_table_returns_columns_and_samples(warehouse: Warehouse) -> None:
    result = await call_tool(warehouse, DESCRIBE_TABLE, {"table": "orders"})
    payload = result.structured_content
    assert payload is not None
    assert [column["name"] for column in payload["columns"]] == [
        "order_id",
        "user_id",
        "ordered_at",
        "amount_cents",
        "status",
    ]
    assert payload["row_count"] > 0
    assert payload["sample_values"]["status"] == ["CANCELLED", "PAID", "REFUNDED"]


@pytest.mark.asyncio
async def test_an_unknown_table_lists_the_tables_that_do_exist(warehouse: Warehouse) -> None:
    result = await call_tool(warehouse, DESCRIBE_TABLE, {"table": "sessions"})
    assert result.is_error is True
    message = text_of(result)
    for table in TABLE_NAMES:
        assert table in message


# -- the story the data tells ----------------------------------------------


@pytest.mark.asyncio
async def test_android_5_2_0_conversion_collapses_while_ios_holds(warehouse: Warehouse) -> None:
    result = await call_tool(warehouse, QUERY_SQL, {"sql": CONVERSION_SQL, "limit": 100})
    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    rates = {
        (platform, version, period): signups / views
        for platform, version, period, views, signups in payload["rows"]
    }

    # Android before the rollout, on both 5.1.x builds, sits at the baseline.
    assert rates[("android", "5.1.0", "before")] == pytest.approx(0.30, abs=0.005)
    assert rates[("android", "5.1.1", "before")] == pytest.approx(0.30, abs=0.005)
    # 5.1.1 does not move after the rollout -- the drop is not "March".
    assert rates[("android", "5.1.1", "after")] == pytest.approx(0.30, abs=0.005)
    # 5.2.0 is where it goes wrong: 12.5%, well under half the baseline.
    assert rates[("android", "5.2.0", "after")] == pytest.approx(0.125, abs=0.005)
    assert rates[("android", "5.2.0", "after")] < rates[("android", "5.1.1", "after")] / 2

    # iOS is unchanged across the same rollout, including on 5.2.0 itself, so
    # the cause is the Android build and not the release.
    ios_before = _pooled(payload["rows"], "ios", "before")
    ios_after = _pooled(payload["rows"], "ios", "after")
    assert ios_before == pytest.approx(0.311, abs=0.005)
    assert ios_after == pytest.approx(ios_before, abs=0.005)
    assert rates[("ios", "5.2.0", "after")] == pytest.approx(0.296, abs=0.005)


@pytest.mark.asyncio
async def test_the_android_drop_is_invisible_without_grouping_by_version(
    warehouse: Warehouse,
) -> None:
    # Pooled over versions the March android rate is 19%, which looks like a
    # platform problem rather than a build problem. The seed has to support
    # that wrong turn or the exercise is not worth running.
    result = await call_tool(warehouse, QUERY_SQL, {"sql": CONVERSION_SQL, "limit": 100})
    payload = result.structured_content
    assert payload is not None
    android_before = _pooled(payload["rows"], "android", "before")
    android_after = _pooled(payload["rows"], "android", "after")
    assert android_before == pytest.approx(0.30, abs=0.005)
    assert android_after == pytest.approx(0.192, abs=0.005)


def _pooled(rows: list[list[Any]], platform: str, period: str) -> float:
    views = sum(row[3] for row in rows if row[0] == platform and row[2] == period)
    signups = sum(row[4] for row in rows if row[0] == platform and row[2] == period)
    return signups / views


def window(start: str, end: str) -> dict[str, str]:
    return {"start_date": start, "end_date": end}


@pytest.mark.asyncio
async def test_conversion_keeps_platform_and_version_grains_separate(warehouse: Warehouse) -> None:
    result = await call_tool(
        warehouse,
        server.GET_REGISTRATION_CONVERSION,
        {
            "windows": [window("2026-02-15", "2026-02-28"), window("2026-03-01", "2026-03-14")],
            "breakdown": "platform_version",
        },
    )
    data = result.structured_content
    assert data and not result.is_error
    assert [(r["signups"], r["views"]) for r in data["overall"]["periods"]] == [
        (406, 1330),
        (336, 1358),
    ]
    assert data["overall"]["change_pp"] == pytest.approx(100 * (336 / 1358 - 406 / 1330))
    assert next(r for r in data["platforms"] if r["platform"] == "ios")["change_pp"] == 0
    versions = {(r["platform"], r["app_version"]): r for r in data["versions"]}
    assert versions["ios", "5.1.1"]["change_pp"] == pytest.approx(100 * (84 / 252 - 112 / 378))
    assert versions["android", "5.2.0"]["change_pp"] is None
    assert versions["android", "5.2.0"]["periods"][0] == {
        "signups": 0,
        "views": 0,
        "rate_pct": None,
    }


@pytest.mark.asyncio
async def test_conversion_single_day_filter_and_offset_boundary(warehouse: Warehouse) -> None:
    # UTC midnight is the exact cut; view-only and signup-only groups both survive.
    with sqlite3.connect(warehouse.path) as db:
        db.executemany(
            "INSERT INTO signup_page_views VALUES (?, ?, ?, ?, ?)",
            [
                (900000, "2028-02-29T23:59:59Z", "ios", "old", "US"),
                (900001, "2028-03-01T01:00:00+01:00", "ios", "new", "US"),
                (900002, "2028-03-01T00:00:00Z", "android", "other", "US"),
            ],
        )
        db.execute(
            "INSERT INTO signups VALUES "
            "(900000, '2028-03-01T00:00:00Z', 'ios', 'signup-only', 'US')"
        )
    result = await call_tool(
        warehouse,
        server.GET_REGISTRATION_CONVERSION,
        {
            "windows": [window("2028-02-29", "2028-02-29"), window("2028-03-01", "2028-03-01")],
            "platform": "ios",
            "breakdown": "platform_version",
        },
    )
    data = result.structured_content
    assert data and [(p["signups"], p["views"]) for p in data["overall"]["periods"]] == [
        (0, 1),
        (1, 1),
    ]
    assert data["overall"]["change_pp"] == 100
    assert (
        next(v for v in data["versions"] if v["app_version"] == "signup-only")["periods"][1][
            "rate_pct"
        ]
        is None
    )
    single = warehouse.registration_conversion([window("2026-03-15", "2026-03-15")], "ios")
    assert single["overall"]["periods"][0]["rate_pct"] == pytest.approx(100 * 14 / 45)
    assert (
        single["overall"]["change_pp"] is None and single["platforms"] == single["versions"] == []
    )


@pytest.mark.parametrize(
    "args",
    [
        {"windows": []},
        {"windows": [window("2026-03-01", "2026-02-28")]},
        {"windows": [window("2026-02-29", "2026-03-01")]},
        {"windows": [window("2026-03-01", "2026-03-02"), window("2026-03-02", "2026-03-03")]},
        {"windows": [window("2026-03-01", "2026-03-01")] * 3},
        {"windows": [window("2026-03-01", "2026-03-01")], "sql": "SELECT 1"},
        {"windows": [window("2026-03-01", "2026-03-01")], "platform": "ios OR 1=1"},
    ],
)
@pytest.mark.asyncio
async def test_conversion_rejects_invalid_date_windows_and_open_filters(
    warehouse: Warehouse, args: dict
) -> None:
    assert (await call_tool(warehouse, server.GET_REGISTRATION_CONVERSION, args)).is_error


@pytest.mark.asyncio
async def test_conversion_large_breakdown_fails_without_partial_totals(
    warehouse: Warehouse,
) -> None:
    from apps.warehouse_mcp.sql_guard import MAX_ROW_CAP

    with sqlite3.connect(warehouse.path) as db:
        db.executemany(
            "INSERT INTO signup_page_views VALUES (?, '2029-05-01T00:00:00Z', 'ios', ?, 'US')",
            [(910000 + i, str(i)) for i in range(MAX_ROW_CAP)],
        )
    args = {"windows": [window("2029-05-01", "2029-05-01")]}
    large = await call_tool(
        warehouse,
        server.GET_REGISTRATION_CONVERSION,
        {
            **args,
            "breakdown": "platform_version",
        },
    )
    assert large.is_error and not large.structured_content
    assert "coarser breakdown" in text_of(large)
    coarse = await call_tool(
        warehouse,
        server.GET_REGISTRATION_CONVERSION,
        {
            **args,
            "breakdown": "platform",
        },
    )
    assert coarse.structured_content["overall"]["periods"][0]["views"] == MAX_ROW_CAP
    empty = warehouse.registration_conversion([window("2029-05-02", "2029-05-02")])
    assert empty["overall"] == {
        "periods": [{"signups": 0, "views": 0, "rate_pct": None}],
        "change_pp": None,
    }


@pytest.mark.asyncio
async def test_paying_user_definition_keeps_failed_first_attempts_out(warehouse: Warehouse) -> None:
    dictionary = await call_tool(warehouse, GET_METRIC_DEFINITION, {"metric": "paying_user"})
    formula = dictionary.structured_content["formula"]
    assert "payments.paid_at, payments.payment_id" in formula
    assert "after selecting the first attempt" in formula

    # A paid retry is a valid sale; the published first-attempt KPI excludes that user.
    result = await call_tool(
        warehouse,
        QUERY_SQL,
        {
            "sql": """
        WITH attempts AS (
            SELECT o.user_id, o.status AS order_status, p.status AS payment_status,
                   row_number() OVER (
                       PARTITION BY o.user_id ORDER BY p.paid_at, p.payment_id
                   ) AS sequence
            FROM signups s JOIN orders o ON o.user_id = s.user_id
            JOIN payments p ON p.order_id = o.order_id
        )
        SELECT count(DISTINCT CASE WHEN sequence = 1 THEN user_id END) AS first_succeeded,
               count(DISTINCT user_id) AS ever_succeeded
        FROM attempts WHERE order_status = 'PAID' AND payment_status = 'SUCCEEDED'
    """
        },
    )
    first, any_success = result.structured_content["rows"][0]
    assert 0 < first < any_success


@pytest.mark.asyncio
async def test_order_activity_classifies_people_before_counting_their_orders(
    warehouse: Warehouse,
) -> None:
    with sqlite3.connect(warehouse.path) as db:
        db.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, 100, ?)",
            [
                (900001, 101, "2028-03-01T00:30:00+01:00", "PAID"),  # Previous UTC day.
                (900002, 101, "2028-03-01T07:00:00Z", "REFUNDED"),
                (900003, 102, "2028-03-01T07:00:00Z", "PAID"),
                (900004, 102, "2028-03-01T08:00:00Z", "REFUNDED"),
                (900005, 103, "2028-03-01T09:00:00Z", "CANCELLED"),
                (900006, 103, "2028-03-01T10:00:00Z", "CANCELLED"),
            ],
        )
    dates = window("2028-03-01", "2028-03-01")
    result = await call_tool(warehouse, server.GET_ORDER_ACTIVITY, dates)
    assert not result.is_error
    assert result.structured_content == {
        "window": dates,
        "ordered_users": 3,
        "with_paid_order": {
            "users": 1,
            "order_rows": 2,
            "paid_rows": 1,
            "non_paid_rows": 1,
            "refunded_rows": 1,
            "cancelled_rows": 0,
            "other_status_rows": 0,
        },
        "without_paid_order": {
            "users": 2,
            "order_rows": 3,
            "paid_rows": 0,
            "non_paid_rows": 3,
            "refunded_rows": 1,
            "cancelled_rows": 2,
            "other_status_rows": 0,
        },
    }
    empty = await call_tool(
        warehouse, server.GET_ORDER_ACTIVITY, window("2029-01-01", "2029-01-01")
    )
    assert empty.structured_content["ordered_users"] == 0
    assert all(n == 0 for n in empty.structured_content["without_paid_order"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"start_date": "2028-03-01", "end_date": "2028-02-29"},
        {"start_date": "2028-03-01' OR 1=1 --", "end_date": "2028-03-01"},
        {"start_date": "2028-03-01", "end_date": "2028-03-01", "status": "PAID"},
    ],
)
async def test_order_activity_rejects_invalid_windows_and_filters(warehouse, args):
    assert (await call_tool(warehouse, server.GET_ORDER_ACTIVITY, args)).is_error


@pytest.mark.asyncio
async def test_paying_user_counts_only_classifies_users_in_the_difference(tmp_path: Path) -> None:
    source = Warehouse(tmp_path / "first-attempts.sqlite3")
    with sqlite3.connect(source.path) as db:
        db.executescript(SCHEMA_SQL)
        db.executemany(
            "INSERT INTO signups VALUES (?, '2026-02-01', 'ios', '1.0', 'US')",
            [(uid,) for uid in range(1, 7)],
        )
    empty = await call_tool(source, server.GET_PAYING_USER_COUNTS)
    assert empty.structured_content == {
        "strict_first_qualified_users": 0,
        "any_qualified_users": 0,
        "any_only_by_first_payment_status": {"failed": 0, "refunded": 0, "other": 0},
    }

    # (payment, user, day, order status, payment status); one order per test attempt.
    attempts = [
        (11, 1, "2026-02-01", "PAID", "FAILED"),
        (12, 1, "2026-02-02", "PAID", "SUCCEEDED"),
        (21, 2, "2026-02-01", "REFUNDED", "REFUNDED"),
        (22, 2, "2026-02-02", "PAID", "SUCCEEDED"),
        (31, 3, "2026-02-01", "PAID", "SUCCEEDED"),  # Only one attempt; included in both.
        (41, 4, "2026-02-01", "REFUNDED", "REFUNDED"),  # Neither count.
        (51, 5, "2026-02-01", "REFUNDED", "SUCCEEDED"),  # Payment alone is insufficient.
        (52, 5, "2026-02-02", "PAID", "SUCCEEDED"),
        (62, 6, "2026-02-01", "PAID", "SUCCEEDED"),  # Equal timestamps, higher ID is later.
        (61, 6, "2026-02-01", "PAID", "FAILED"),
        (70, 999, "2026-02-01", "PAID", "SUCCEEDED"),  # Unregistered.
    ]
    with sqlite3.connect(source.path) as db:
        for pid, uid, day, order_status, payment_status in attempts:
            db.execute("INSERT INTO orders VALUES (?, ?, ?, 100, ?)", (pid, uid, day, order_status))
            db.execute(
                "INSERT INTO payments VALUES (?, ?, ?, 100, ?)", (pid, pid, day, payment_status)
            )
    result = await call_tool(source, server.GET_PAYING_USER_COUNTS)
    assert not result.is_error
    assert result.structured_content == {
        "strict_first_qualified_users": 1,
        "any_qualified_users": 5,
        "any_only_by_first_payment_status": {"failed": 2, "refunded": 1, "other": 1},
    }


@pytest.mark.asyncio
async def test_paying_user_counts_seed_and_rejection_of_filters(warehouse: Warehouse) -> None:
    result = await call_tool(warehouse, server.GET_PAYING_USER_COUNTS)
    assert result.structured_content == {
        "strict_first_qualified_users": 368,
        "any_qualified_users": 405,
        "any_only_by_first_payment_status": {"failed": 31, "refunded": 6, "other": 0},
    }
    for arguments in ({"start_date": "2026-01-01"}, {"sql": "SELECT 1"}):
        denied = await call_tool(warehouse, server.GET_PAYING_USER_COUNTS, arguments)
        assert denied.is_error and denied.structured_content is None
