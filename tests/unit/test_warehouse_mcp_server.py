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
from apps.warehouse_mcp.warehouse import METRICS, TABLE_NAMES, Warehouse

EXPECTED_TOOLS = (GET_METRIC_DEFINITION, LIST_TABLES, DESCRIBE_TABLE, QUERY_SQL)

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
async def test_the_tool_list_is_exactly_the_four_read_tools(warehouse: Warehouse) -> None:
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
    result = await call_tool(warehouse, GET_METRIC_DEFINITION, {"metric": metric})
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
