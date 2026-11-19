"""Registration conversion evidence is a tool result, including when cited in a finding."""

from uuid import uuid4

import pytest

from apps.warehouse_mcp.warehouse import Warehouse
from packages.agent_runtime.work_layer import RecordedToolCall
from packages.artifacts.projections import project_analysis_query
from packages.artifacts.schemas import (
    ANALYSIS_FINDING,
    ANALYSIS_QUERY_RESULT,
    validate_artifact_content,
)
from packages.core.errors.exceptions import AgentHubError


def test_paying_user_metric_round_trips_in_a_finding(tmp_path):
    warehouse = Warehouse(tmp_path / "finance.sqlite3")
    warehouse.build()
    call = RecordedToolCall(
        "warehouse.get_paying_user_counts",
        "finance",
        4,
        arguments={},
        data={"structured_content": warehouse.paying_user_counts()},
    )
    artifact = project_analysis_query(call, uuid4())
    assert artifact is not None
    finding = validate_artifact_content(
        ANALYSIS_FINDING,
        {"question": "Which users qualify?", "conclusion": "368", "queries": [artifact.content]},
    )
    saved = finding["queries"][0]
    assert saved["metric_arguments"] == {}
    assert dict(saved["rows"]) == {
        "strict_first_qualified_users": 368,
        "any_qualified_users": 405,
        "any_only_first_failed": 31,
        "any_only_first_refunded": 6,
        "any_only_first_other": 0,
    }
    with pytest.raises(AgentHubError):
        validate_artifact_content(artifact.type, {**saved, "metric_arguments": {"window": "all"}})


def test_order_activity_finding_keeps_the_observed_cohorts_and_window(tmp_path):
    warehouse = Warehouse(tmp_path / "orders.sqlite3")
    warehouse.build()
    window = {"start_date": "2026-03-14", "end_date": "2026-03-16"}
    result = warehouse.order_activity(window)
    call = RecordedToolCall(
        "warehouse.get_order_activity",
        "call-orders",
        2,
        arguments=window,
        data={"structured_content": result},
    )
    artifact = project_analysis_query(call, uuid4())
    assert artifact is not None
    content = validate_artifact_content(artifact.type, artifact.content)
    assert content["metric_arguments"] == window
    assert content["rows"] == [
        ["with_paid_order", 42, 47, 46, 1, 1, 0, 0],
        ["without_paid_order", 5, 5, 0, 5, 3, 2, 0],
    ]
    finding = validate_artifact_content(
        ANALYSIS_FINDING,
        {"question": "Who ordered?", "conclusion": "47 users", "queries": [content]},
    )
    assert finding["queries"] == [content]
    assert content["provenance"]["tool_call_id"] == "call-orders"
    bad_window = {"start_date": "2026-03-15", "end_date": "2026-03-14"}
    with pytest.raises(AgentHubError):
        validate_artifact_content(artifact.type, {**content, "metric_arguments": bad_window})
    result["with_paid_order"]["users"] = None
    assert project_analysis_query(call, uuid4()) is None
    result["window"] = bad_window
    assert project_analysis_query(call, uuid4()) is None


def test_metric_tool_result_and_finding_preserve_actual_arguments_and_rows(tmp_path):
    warehouse = Warehouse(tmp_path / "metric.sqlite3")
    warehouse.build()
    args = {
        "windows": [
            {"start_date": "2026-03-14", "end_date": "2026-03-14"},
            {"start_date": "2026-03-15", "end_date": "2026-03-15"},
        ],
        "platform": "ios",
        "breakdown": "platform_version",
    }
    result = warehouse.registration_conversion(**args)
    call = RecordedToolCall(
        "warehouse.get_registration_conversion",
        "call-rate",
        8,
        arguments=args,
        data={"structured_content": result},
    )
    run = uuid4()
    artifact = project_analysis_query(call, run)
    assert artifact is not None and artifact.type == ANALYSIS_QUERY_RESULT
    content = validate_artifact_content(artifact.type, artifact.content)
    assert content["metric_arguments"] == args
    assert "sql" not in content
    assert content["provenance"] == dict(
        run_id=str(run), tool_call_id="call-rate", tool_identity=call.tool_identity, step_sequence=8
    )
    rows = [dict(zip(content["columns"], row, strict=True)) for row in content["rows"]]
    total = next(
        row for row in rows if row["scope"] == "overall" and row["start_date"] == "2026-03-15"
    )
    assert (total["signups"], total["views"]) == (14, 45)
    assert total["rate_pct"] == result["overall"]["periods"][1]["rate_pct"]
    assert total["change_pp"] == result["overall"]["change_pp"]
    assert any(row["scope"] == "version" and row["app_version"] == "5.2.0" for row in rows)
    citation = validate_artifact_content(
        ANALYSIS_FINDING,
        {
            "question": "What was iOS conversion?",
            "conclusion": "14 registrations from 45 views.",
            "queries": [content],
        },
    )
    assert citation["queries"] == [content]
    with pytest.raises(AgentHubError):
        validate_artifact_content(artifact.type, {**content, "sql": "SELECT 1"})
    result["windows"] = []
    assert project_analysis_query(call, run) is None


def test_two_window_metric_caps_evidence_after_overall_rows_and_retains_true_count():
    windows = [
        {"start_date": "2026-03-14", "end_date": "2026-03-14"},
        {"start_date": "2026-03-15", "end_date": "2026-03-15"},
    ]
    totals = {
        "periods": [
            {"signups": 1, "views": 4, "rate_pct": 25.0},
            {"signups": 2, "views": 4, "rate_pct": 50.0},
        ],
        "change_pp": 25.0,
    }
    result = {
        "windows": windows,
        "overall": totals,
        "platforms": [{"platform": "ios", **totals}],
        "versions": [{"platform": "ios", "app_version": str(i), **totals} for i in range(1000)],
    }
    call = RecordedToolCall(
        "get_registration_conversion",
        "many-versions",
        1,
        arguments={"windows": windows, "breakdown": "platform_version"},
        data={"structured_content": result},
    )
    artifact = project_analysis_query(call, uuid4())
    assert artifact is not None
    content = validate_artifact_content(artifact.type, artifact.content)
    assert content["truncated"] is True and content["row_count"] == 2004
    assert len(content["rows"]) == 500
    assert [row[0] for row in content["rows"][:4]] == ["overall", "overall", "platform", "platform"]
    assert content["rows"][0][-1] is None and content["rows"][1][-1] == 25.0
    assert content["rows"][-2][2] == content["rows"][-1][2]
    finding = validate_artifact_content(
        ANALYSIS_FINDING,
        {
            "question": "Where did conversion change?",
            "conclusion": "Compare the retained versions.",
            "queries": [content],
        },
    )
    assert finding["queries"][0] == content


@pytest.mark.parametrize(
    "args",
    [
        {"windows": [{"start_date": "2026-99-99", "end_date": "2026-99-99"}]},
        {"windows": [{"start_date": "2026-03-16", "end_date": "2026-03-15"}]},
        {
            "windows": [
                {"start_date": "2026-03-15", "end_date": "2026-03-16"},
                {"start_date": "2026-03-16", "end_date": "2026-03-18"},
            ]
        },
        {"windows": [{"start_date": "2026-03-15", "end_date": "2026-03-15"}], "platform": None},
    ],
)
def test_finding_rejects_invalid_metric_arguments(args):
    content = {
        "question": "Q",
        "conclusion": "C",
        "queries": [
            {
                "metric_arguments": args,
                "columns": ["views"],
                "rows": [[45]],
                "provenance": {
                    "run_id": str(uuid4()),
                    "tool_identity": "get_registration_conversion",
                    "tool_call_id": "lookup",
                    "step_sequence": 1,
                },
            }
        ],
    }
    with pytest.raises(AgentHubError) as caught:
        validate_artifact_content(ANALYSIS_FINDING, content)
    assert caught.value.code == "ARTIFACT_INVALID"
