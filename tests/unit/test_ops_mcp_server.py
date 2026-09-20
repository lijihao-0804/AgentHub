"""Unit tests for the Ops MCP Server.

Nothing here touches the network — there is nothing to touch, since the whole
dataset is in memory. What is being pinned down is the frozen contract (the
tool list, the schemas, the annotations) and the four claims the fixture has to
support for an investigation to be possible at all: a baseline before the
deploy, a rise across it, silence outside the scripted window, and a diff that
carries the cause.
"""

from __future__ import annotations

from typing import Any

import mcp_types
import pytest

from apps.ops_mcp import incident_data, server
from apps.ops_mcp.incident_data import BAD_COMMIT_SHA, IncidentDataError

BEFORE = ("2026-03-14T20:50:00Z", "2026-03-14T21:00:00Z")
ACROSS = ("2026-03-14T20:50:00Z", "2026-03-14T21:20:00Z")
ELSEWHERE = ("2026-03-15T03:00:00Z", "2026-03-15T04:00:00Z")

EXPECTED_TOOLS = (
    "query_metrics",
    "query_logs",
    "get_deployments",
    "get_commit",
    "rollback_deployment",
)


async def _call(
    name: str, arguments: dict[str, Any], *, rollback_mode: str = server.DEFAULT_ROLLBACK_MODE
) -> mcp_types.CallToolResult:
    """Drive one tool call through the registered handler.

    The handler is reached through the server's registry rather than called
    directly, so these tests exercise the dispatch and the error trapping a
    real client goes through, not just the query functions underneath.
    """

    built = server.build_server(rollback_mode=rollback_mode)
    handler = built._request_handlers["tools/call"].handler
    return await handler(None, mcp_types.CallToolRequestParams(name=name, arguments=arguments))


def _structured(result: mcp_types.CallToolResult) -> dict[str, Any]:
    assert result.is_error is False, _text(result)
    assert result.structured_content is not None
    return dict(result.structured_content)


def _text(result: mcp_types.CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


# -- the frozen contract ----------------------------------------------------


def test_the_tool_list_is_exactly_those_five() -> None:
    assert tuple(tool.name for tool in server.TOOLS) == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_list_tools_returns_the_same_five_over_the_wire() -> None:
    built = server.build_server()
    handler = built._request_handlers["tools/list"].handler
    result = await handler(None, None)

    assert tuple(tool.name for tool in result.tools) == EXPECTED_TOOLS


@pytest.mark.parametrize("tool", server.TOOLS, ids=[tool.name for tool in server.TOOLS])
def test_every_schema_closes_the_door_on_extra_properties(tool: mcp_types.Tool) -> None:
    # Checked recursively: a nested object that accepted extra properties would
    # let an unexpected field into a record AgentHub validates as frozen.
    assert tool.output_schema is not None
    for schema in (tool.input_schema, tool.output_schema):
        for node in _objects(schema):
            assert node.get("additionalProperties") is False


@pytest.mark.parametrize("tool", server.TOOLS, ids=[tool.name for tool in server.TOOLS])
def test_every_output_field_is_required(tool: mcp_types.Tool) -> None:
    # A field that may be missing forces a consumer to tell "absent" from
    # "unknown", which it cannot do. Nullable-and-present is the contract.
    for node in _objects(tool.output_schema or {}):
        assert set(node.get("required") or ()) == set(node.get("properties") or {})


def _objects(schema: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if schema.get("type") == "object":
        found.append(schema)
        for child in (schema.get("properties") or {}).values():
            found.extend(_objects(child))
    items = schema.get("items")
    if isinstance(items, dict):
        found.extend(_objects(items))
    return found


@pytest.mark.parametrize(
    ("schema", "arguments", "expected"),
    [
        (server.QUERY_METRICS_INPUT_SCHEMA, {"service": "checkout-api"}, None),
        (server.QUERY_METRICS_INPUT_SCHEMA, {"service": "a", "region": "eu"}, "region"),
        (server.QUERY_LOGS_INPUT_SCHEMA, {"service": "a", "limit": 10}, None),
        (server.QUERY_LOGS_INPUT_SCHEMA, {"service": "a", "severity": "high"}, "severity"),
        (server.GET_COMMIT_INPUT_SCHEMA, {"commit_sha": "abc"}, None),
        (server.GET_COMMIT_INPUT_SCHEMA, {"commit_sha": "abc", "branch": "main"}, "branch"),
        (server.ROLLBACK_INPUT_SCHEMA, {"service": "a", "force": True}, "force"),
    ],
)
def test_unknown_arguments_are_refused_not_dropped(
    schema: dict[str, Any], arguments: dict[str, Any], expected: str | None
) -> None:
    # Dropping `region` silently would tell the caller it scoped to a region.
    if expected is None:
        server._reject_unknown(dict(arguments), schema)
        return
    with pytest.raises(IncidentDataError) as error:
        server._reject_unknown(dict(arguments), schema)
    assert expected in str(error.value)


@pytest.mark.asyncio
async def test_an_unknown_argument_is_an_error_result_not_an_exception() -> None:
    result = await _call(
        "query_metrics",
        {
            "service": "checkout-api",
            "metric": "http_5xx_rate",
            "from_time": BEFORE[0],
            "to_time": BEFORE[1],
            "region": "eu-west-1",
        },
    )

    assert result.is_error is True
    assert "region" in _text(result)


# -- governance annotations -------------------------------------------------


@pytest.mark.parametrize("name", EXPECTED_TOOLS[:4])
def test_the_four_read_tools_are_annotated_read_only(name: str) -> None:
    annotations = _tool(name).annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True
    assert annotations.open_world_hint is False


def test_rollback_is_annotated_destructive_and_not_read_only() -> None:
    tool = _tool("rollback_deployment")
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is False
    # The description is what an approving human reads. It has to say what the
    # call does to production without their having to know what a rollback is.
    assert "production" in (tool.description or "")


def _tool(name: str) -> mcp_types.Tool:
    return next(tool for tool in server.TOOLS if tool.name == name)


def test_an_unrecognised_rollback_mode_stops_the_server_at_startup() -> None:
    # Treating it as "succeed" would let someone who meant to arm the hang path
    # read a green run as proof the timeout path works.
    with pytest.raises(SystemExit):
        server.build_server(rollback_mode="pretend")


# -- the incident is actually in the data -----------------------------------


@pytest.mark.asyncio
async def test_a_window_before_the_deploy_shows_the_baseline() -> None:
    payload = _structured(
        await _call(
            "query_metrics",
            {
                "service": "checkout-api",
                "metric": "http_5xx_rate",
                "from_time": BEFORE[0],
                "to_time": BEFORE[1],
            },
        )
    )

    assert payload["unit"] == "percent"
    # One point per minute across an eleven-minute inclusive window.
    assert len(payload["points"]) == 11
    assert payload["points"][0]["timestamp"] == "2026-03-14T20:50:00Z"
    assert payload["summary"]["baseline"] == 1.9
    assert payload["summary"]["max"] < 3
    assert all(point["value"] < 3 for point in payload["points"])


@pytest.mark.asyncio
async def test_a_window_spanning_the_incident_shows_the_rise() -> None:
    payload = _structured(
        await _call(
            "query_metrics",
            {
                "service": "checkout-api",
                "metric": "http_5xx_rate",
                "from_time": ACROSS[0],
                "to_time": ACROSS[1],
            },
        )
    )

    assert len(payload["points"]) == 31
    assert payload["summary"]["baseline"] == 1.9
    assert payload["summary"]["max"] > 30
    at_2103 = next(
        point for point in payload["points"] if point["timestamp"] == "2026-03-14T21:03:00Z"
    )
    assert at_2103["value"] == 31.0


@pytest.mark.asyncio
async def test_db_connection_errors_start_at_zero_and_climb_into_the_hundreds() -> None:
    payload = _structured(
        await _call(
            "query_metrics",
            {
                "service": "checkout-api",
                "metric": "db_connection_errors",
                "from_time": ACROSS[0],
                "to_time": ACROSS[1],
            },
        )
    )

    by_time = {point["timestamp"]: point["value"] for point in payload["points"]}
    assert by_time["2026-03-14T20:58:00Z"] == 0
    assert by_time["2026-03-14T21:04:00Z"] >= 100


@pytest.mark.asyncio
async def test_latency_moves_last_at_2106() -> None:
    payload = _structured(
        await _call(
            "query_metrics",
            {
                "service": "checkout-api",
                "metric": "p95_latency_ms",
                "from_time": ACROSS[0],
                "to_time": ACROSS[1],
            },
        )
    )

    by_time = {point["timestamp"]: point["value"] for point in payload["points"]}
    assert by_time["2026-03-14T20:58:00Z"] == 428
    assert by_time["2026-03-14T21:06:00Z"] == 3800


@pytest.mark.asyncio
async def test_a_window_outside_the_data_returns_nothing_rather_than_invented_points() -> None:
    payload = _structured(
        await _call(
            "query_metrics",
            {
                "service": "checkout-api",
                "metric": "http_5xx_rate",
                "from_time": ELSEWHERE[0],
                "to_time": ELSEWHERE[1],
            },
        )
    )

    assert payload["points"] == []
    # Nulls, not zeroes: this server observed nothing there, which is not the
    # same claim as having observed a flat zero.
    assert payload["summary"] == {"min": None, "max": None, "baseline": None, "current": None}


@pytest.mark.asyncio
async def test_the_logs_carry_the_pool_exhaustion_from_2104() -> None:
    payload = _structured(
        await _call(
            "query_logs",
            {
                "service": "checkout-api",
                "from_time": "2026-03-14T21:04:00Z",
                "to_time": "2026-03-14T21:05:00Z",
            },
        )
    )

    messages = [entry["message"] for entry in payload["entries"]]
    assert any("connection pool exhausted" in message for message in messages)
    assert any("psycopg.PoolTimeout" in message for message in messages)


@pytest.mark.asyncio
async def test_a_log_query_filters_and_reports_the_untruncated_total() -> None:
    payload = _structured(
        await _call(
            "query_logs",
            {
                "service": "checkout-api",
                "query": "FATAL",
                "from_time": ACROSS[0],
                "to_time": ACROSS[1],
                "limit": 2,
            },
        )
    )

    assert payload["query"] == "FATAL"
    assert len(payload["entries"]) == 2
    # The page is two entries; the total is what matched, so the caller knows
    # there is more rather than believing it read everything.
    assert payload["total"] > 2
    assert {entry["level"] for entry in payload["entries"]} == {"FATAL"}


@pytest.mark.asyncio
async def test_an_absent_log_query_comes_back_null() -> None:
    payload = _structured(
        await _call(
            "query_logs",
            {"service": "checkout-api", "from_time": ACROSS[0], "to_time": ACROSS[1]},
        )
    )

    assert payload["query"] is None


@pytest.mark.asyncio
async def test_the_deploy_and_its_predecessor_are_both_findable() -> None:
    payload = _structured(
        await _call(
            "get_deployments",
            {
                "service": "checkout-api",
                "from_time": "2026-03-13T00:00:00Z",
                "to_time": "2026-03-14T23:59:00Z",
            },
        )
    )

    versions = [record["version"] for record in payload["deployments"]]
    assert versions == ["5.4.2", "5.4.1"]
    newest = payload["deployments"][0]
    assert newest["deployed_at"] == "2026-03-14T21:01:00Z"
    assert newest["deployed_by"] == "chen.wei"
    assert newest["status"] == "SUCCEEDED"
    assert newest["environment"] == "production"
    assert newest["commit_sha"] == BAD_COMMIT_SHA


@pytest.mark.asyncio
async def test_get_commit_surfaces_the_pool_configuration_change() -> None:
    payload = _structured(await _call("get_commit", {"commit_sha": BAD_COMMIT_SHA}))

    changed = {record["path"]: record for record in payload["files_changed"]}
    assert "config/database.yaml" in changed
    diff = changed["config/database.yaml"]["diff"]
    # The cause is only ever stated as a diff. If this assertion stops holding,
    # the incident has become undiscoverable and the fixture is worthless.
    assert "-  pool_max_size: 50" in diff
    assert "+  pool_max_size: 5" in diff
    # A file with no diff carries null, not "".
    assert changed["docs/CHANGELOG.md"]["diff"] is None


@pytest.mark.asyncio
async def test_no_tool_output_names_the_cause_outside_the_diff() -> None:
    # The investigation has to reach the diff. A metric name, a log line or a
    # deployment record that mentioned the pool size would let a run look
    # competent without having walked the chain.
    for name, arguments in (
        (
            "query_metrics",
            {
                "service": "checkout-api",
                "metric": "db_connection_errors",
                "from_time": ACROSS[0],
                "to_time": ACROSS[1],
            },
        ),
        (
            "query_logs",
            {"service": "checkout-api", "from_time": ACROSS[0], "to_time": ACROSS[1], "limit": 200},
        ),
        (
            "get_deployments",
            {
                "service": "checkout-api",
                "from_time": "2026-03-13T00:00:00Z",
                "to_time": "2026-03-14T23:59:00Z",
            },
        ),
    ):
        payload = _structured(await _call(name, arguments))
        assert "pool_max_size" not in str(payload)


@pytest.mark.asyncio
async def test_a_short_sha_prefix_resolves_to_the_same_commit() -> None:
    payload = _structured(await _call("get_commit", {"commit_sha": "abc123d"}))

    assert payload["commit_sha"] == BAD_COMMIT_SHA


# -- refusing to invent -----------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_service_is_an_error_result_not_an_exception() -> None:
    result = await _call(
        "query_metrics",
        {
            "service": "payments-api",
            "metric": "http_5xx_rate",
            "from_time": ACROSS[0],
            "to_time": ACROSS[1],
        },
    )

    assert result.is_error is True
    assert "payments-api" in _text(result)
    assert result.structured_content is None


@pytest.mark.asyncio
async def test_an_unknown_commit_is_an_error_result() -> None:
    result = await _call("get_commit", {"commit_sha": "0" * 40})

    assert result.is_error is True


@pytest.mark.asyncio
async def test_an_unknown_rollback_target_is_an_error_result() -> None:
    result = await _call(
        "rollback_deployment",
        {"service": "checkout-api", "target_version": "4.0.0", "reason": "incident"},
    )

    assert result.is_error is True
    assert "4.0.0" in _text(result)


@pytest.mark.asyncio
async def test_an_unknown_tool_name_is_an_error_result() -> None:
    result = await _call("drop_database", {})

    assert result.is_error is True


def test_an_inverted_window_is_refused_rather_than_swapped() -> None:
    with pytest.raises(IncidentDataError):
        incident_data.query_metrics(
            service="checkout-api",
            metric="http_5xx_rate",
            from_time=ACROSS[1],
            to_time=ACROSS[0],
        )


# -- the write tool ---------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_in_succeed_mode_returns_a_completed_record() -> None:
    payload = _structured(
        await _call(
            "rollback_deployment",
            {
                "service": "checkout-api",
                "target_version": "5.4.1",
                "reason": "5xx rate at 37% since the 5.4.2 deploy",
            },
        )
    )

    assert payload["rolled_back_from"] == "5.4.2"
    assert payload["rolled_back_to"] == "5.4.1"
    assert payload["status"] == "COMPLETED"
    assert payload["deployment_id"]
    assert payload["started_at"].endswith("Z")


@pytest.mark.asyncio
async def test_rollback_in_hang_mode_never_answers() -> None:
    # The point of the mode is that the caller, not the server, decides when to
    # give up. A short timeout here stands in for the governed runtime's.
    with pytest.raises(TimeoutError):
        await _wait_briefly(
            _call(
                "rollback_deployment",
                {
                    "service": "checkout-api",
                    "target_version": "5.4.1",
                    "reason": "exercising the timeout path",
                },
                rollback_mode="hang",
            )
        )


async def _wait_briefly(awaitable: Any) -> Any:
    import asyncio

    return await asyncio.wait_for(awaitable, timeout=0.2)
