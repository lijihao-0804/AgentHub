"""The artifact types the three new applications add, and the one recorder.

The claim under test is not that incidents work. It is that adding an
application costs a projection and a validator, and that the rule holding the
research artifacts honest -- nothing lands in the record that cannot be traced
to a tool call -- was written once rather than four times.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.work_layer import RecordedToolCall
from packages.artifacts import schemas
from packages.artifacts.projections import (
    project_analysis_query,
    project_incident_timeline,
    project_paper_search,
    tool_tail,
)
from packages.artifacts.recorder import ToolResultArtifactRecorder
from packages.core.errors.exceptions import AgentHubError

RUN_ID = UUID("11111111-1111-1111-1111-111111111111")


def call(tool: str, structured: Any, arguments: dict[str, Any] | None = None) -> RecordedToolCall:
    return RecordedToolCall(
        tool_identity=tool,
        tool_call_id="call-1",
        step_sequence=5,
        arguments=arguments or {},
        data={"structured_content": structured},
    )


METRICS = {
    "service": "checkout-api",
    "metric": "http_5xx_rate",
    "unit": "%",
    "points": [
        {"timestamp": "2026-03-14T20:58:00Z", "value": 2.0},
        {"timestamp": "2026-03-14T21:03:00Z", "value": 31.0},
    ],
    "summary": {"min": 2.0, "max": 31.0, "baseline": 2.0, "current": 31.0},
}

DEPLOYMENTS = {
    "service": "checkout-api",
    "total": 1,
    "deployments": [
        {
            "deployment_id": "dep-77",
            "version": "5.4.2",
            "commit_sha": "abc123def4567890",
            "deployed_at": "2026-03-14T21:01:00Z",
            "deployed_by": "ci",
            "status": "SUCCEEDED",
            "environment": "production",
        }
    ],
}

ROLLBACK = {
    "service": "checkout-api",
    "rolled_back_from": "5.4.2",
    "rolled_back_to": "5.4.1",
    "deployment_id": "dep-78",
    "started_at": "2026-03-14T21:20:00Z",
    "status": "IN_PROGRESS",
}

QUERY_SQL = {
    "sql": "SELECT platform, count(*) FROM signups GROUP BY platform",
    "columns": ["platform", "count"],
    "rows": [["android", 312], ["ios", 454]],
    "row_count": 2,
    "truncated": False,
    "elapsed_ms": 4.5,
}


# --------------------------------------------------------------------------
# type registry


def test_the_type_whitelist_and_the_validator_table_agree() -> None:
    # A type in one and not the other is a 422 nobody can explain: either a
    # listed type that cannot be written, or a writable type nothing declares.
    assert schemas.ARTIFACT_TYPES == frozenset(schemas._VALIDATORS)


def test_an_unlisted_type_is_refused() -> None:
    with pytest.raises(AgentHubError) as error:
        schemas.validate_artifact_content("incident.guess", {})
    assert error.value.code == schemas.ARTIFACT_TYPE_UNKNOWN


# --------------------------------------------------------------------------
# incident


def test_a_recorded_timeline_entry_without_provenance_is_refused() -> None:
    """The same rule as a searched paper, applied to a different application.

    An entry with no traceable call is an event nobody observed, and the point
    of recording rather than asking the model to write is that there is no way
    to put one there.
    """

    with pytest.raises(AgentHubError) as error:
        schemas.validate_artifact_content(
            schemas.INCIDENT_TIMELINE,
            {"entries": [{"at": "2026-03-14T21:01:00Z", "kind": "DEPLOYMENT", "summary": "5.4.2"}]},
        )
    assert error.value.code == schemas.ARTIFACT_INVALID


def test_a_report_may_hold_entries_without_provenance() -> None:
    # A report is assembled by a person out of what the thread already holds,
    # so its timeline is a copy and the copy carries no fresh call.
    content = schemas.validate_artifact_content(
        schemas.INCIDENT_REPORT,
        {
            "incident_ref": "INC-2026-091",
            "severity": "SEV-2",
            "summary": "Checkout errors after 5.4.2.",
            "timeline": [{"at": "21:01", "kind": "DEPLOYMENT", "summary": "5.4.2 deployed"}],
            "root_cause": "Connection pool reduced from 50 to 5.",
            "actions_taken": ["Rolled back to 5.4.1."],
            "remaining_risks": ["The config change is still on main."],
        },
    )
    assert content["timeline"][0]["kind"] == "DEPLOYMENT"
    assert "provenance" not in content["timeline"][0]


def test_an_unknown_timeline_kind_is_refused() -> None:
    with pytest.raises(AgentHubError):
        schemas.validate_artifact_content(
            schemas.INCIDENT_REPORT,
            {"summary": "x", "timeline": [{"at": "21:01", "kind": "HUNCH", "summary": "y"}]},
        )


def test_a_report_requires_a_summary() -> None:
    with pytest.raises(AgentHubError):
        schemas.validate_artifact_content(schemas.INCIDENT_REPORT, {"root_cause": "pool size"})


@pytest.mark.parametrize(
    ("tool", "structured", "kind", "at"),
    [
        ("ops.query_metrics", METRICS, "METRIC", "2026-03-14T20:58:00Z"),
        ("ops.get_deployments", DEPLOYMENTS, "DEPLOYMENT", "2026-03-14T21:01:00Z"),
        ("ops.rollback_deployment", ROLLBACK, "ACTION", "2026-03-14T21:20:00Z"),
    ],
)
def test_each_ops_tool_projects_to_its_own_kind_of_entry(
    tool: str, structured: dict[str, Any], kind: str, at: str
) -> None:
    built = project_incident_timeline(call(tool, structured), RUN_ID)
    assert built is not None
    assert built.type == schemas.INCIDENT_TIMELINE
    entry = built.content["entries"][0]
    assert entry["kind"] == kind
    assert entry["at"] == at
    assert entry["provenance"]["tool_identity"] == tool
    assert entry["provenance"]["step_sequence"] == 5
    # Validation is what actually admits it; the projection only proposes.
    schemas.validate_artifact_content(built.type, built.content)


def test_the_metric_entry_reports_the_numbers_and_not_a_diagnosis() -> None:
    """A projection records; it does not conclude.

    Deciding that 21:03 is when it broke is the investigator's job. If this
    function did it, the finding would arrive stamped with provenance it does
    not have -- the tool returned points, not a cause.
    """

    built = project_incident_timeline(call("ops.query_metrics", METRICS), RUN_ID)
    assert built is not None
    summary = built.content["entries"][0]["summary"]
    assert "2.0 to 31.0" in summary
    for word in ("caused", "because", "spike", "regression"):
        assert word not in summary.lower()


def test_a_metric_window_with_no_points_produces_nothing() -> None:
    empty = {**METRICS, "points": [], "summary": {"min": None, "max": None, "baseline": None}}
    assert project_incident_timeline(call("ops.query_metrics", empty), RUN_ID) is None


def test_a_log_query_keeps_one_entry_per_line() -> None:
    logs = {
        "service": "checkout-api",
        "query": "timeout",
        "total": 2,
        "entries": [
            {
                "timestamp": "2026-03-14T21:04:00Z",
                "level": "ERROR",
                "service": "checkout-api",
                "message": "db connection timeout",
                "count": 212,
            },
            {
                "timestamp": "2026-03-14T21:05:00Z",
                "level": "ERROR",
                "service": "checkout-api",
                "message": "pool exhausted",
                "count": 90,
            },
        ],
    }
    built = project_incident_timeline(call("ops.query_logs", logs), RUN_ID)
    assert built is not None
    entries = built.content["entries"]
    assert [entry["at"] for entry in entries] == [
        "2026-03-14T21:04:00Z",
        "2026-03-14T21:05:00Z",
    ]
    assert entries[0]["detail"] == "count 212"


def test_a_commit_entry_names_the_files_without_carrying_the_diff() -> None:
    commit = {
        "commit_sha": "abc123def4567890",
        "repository": "acme/checkout",
        "author": "dana",
        "committed_at": "2026-03-14T18:40:00Z",
        "message": "tune the pool\n\nlonger body",
        "files_changed": [
            {
                "path": "config/database.yaml",
                "additions": 2,
                "deletions": 2,
                "diff": "-  pool_max_size: 50\n+  pool_max_size: 5",
            }
        ],
    }
    built = project_incident_timeline(call("ops.get_commit", commit), RUN_ID)
    assert built is not None
    entry = built.content["entries"][0]
    assert entry["summary"] == "abc123def4 tune the pool"
    assert "config/database.yaml +2/-2" in entry["detail"]
    assert "pool_max_size" not in entry["detail"]


# --------------------------------------------------------------------------
# analysis


def test_a_query_result_is_recorded_whole_with_provenance() -> None:
    built = project_analysis_query(call("wh.query_sql", QUERY_SQL), RUN_ID)
    assert built is not None
    assert built.type == schemas.ANALYSIS_QUERY_RESULT
    content = schemas.validate_artifact_content(built.type, built.content)
    assert content["columns"] == ["platform", "count"]
    assert content["rows"] == [["android", 312], ["ios", 454]]
    assert content["provenance"]["run_id"] == str(RUN_ID)
    assert built.title.startswith("Query: SELECT platform")


def test_a_query_result_without_provenance_is_refused() -> None:
    with pytest.raises(AgentHubError):
        schemas.validate_artifact_content(
            schemas.ANALYSIS_QUERY_RESULT,
            {"sql": "SELECT 1", "columns": ["n"], "rows": [[1]]},
        )


def test_a_row_that_does_not_match_the_columns_is_refused() -> None:
    # A ragged table renders as a table and means nothing, which is worse than
    # an error because nobody notices.
    with pytest.raises(AgentHubError):
        schemas.validate_artifact_content(
            schemas.ANALYSIS_QUERY_RESULT,
            {
                "sql": "SELECT 1",
                "columns": ["a", "b"],
                "rows": [[1]],
                "provenance": {
                    "run_id": str(RUN_ID),
                    "tool_call_id": "c",
                    "tool_identity": "wh.query_sql",
                },
            },
        )


def test_a_finding_carries_its_queries() -> None:
    content = schemas.validate_artifact_content(
        schemas.ANALYSIS_FINDING,
        {
            "question": "Why did registration conversion fall?",
            "conclusion": "Android 5.2.0 converts at 12.5% against a 30% baseline.",
            "evidence": ["iOS is flat over the same window."],
            "queries": [
                {
                    "sql": QUERY_SQL["sql"],
                    "columns": QUERY_SQL["columns"],
                    "rows": QUERY_SQL["rows"],
                    "provenance": {
                        "run_id": str(RUN_ID),
                        "tool_call_id": "c",
                        "tool_identity": "wh.query_sql",
                    },
                }
            ],
        },
    )
    assert content["queries"][0]["rows"][0][0] == "android"


# --------------------------------------------------------------------------
# support


def test_a_handoff_must_say_what_should_happen_next() -> None:
    """A handoff with no recommendation moves work without advancing it.

    That is the failure escalation exists to avoid: a human opens the case and
    is exactly where the customer was, minus the time it took to get there.
    """

    with pytest.raises(AgentHubError):
        schemas.validate_artifact_content(
            schemas.SUPPORT_HANDOFF,
            {"problem": "Refund has not arrived.", "checked": ["ORD-77412"]},
        )


def test_a_handoff_keeps_what_was_already_checked() -> None:
    content = schemas.validate_artifact_content(
        schemas.SUPPORT_HANDOFF,
        {
            "customer_ref": "CUS-10344",
            "case_ref": "CS-10291",
            "problem": "Refund approved three weeks ago has not arrived.",
            "checked": ["ORD-77412 is RETURNED", "REF-5512 is APPROVED, issued_at is null"],
            "findings": ["The refund was never issued, so no money has moved."],
            "recommended_action": "Re-issue REF-5512 manually and confirm with the customer.",
            "reason": "Re-issuing money is not an action to take automatically.",
        },
    )
    assert len(content["checked"]) == 2
    assert content["recommended_action"].startswith("Re-issue")


# --------------------------------------------------------------------------
# the shared recorder


@pytest.mark.parametrize(
    ("identity", "tail"),
    [("query_sql", "query_sql"), ("warehouse.query_sql", "query_sql"), ("a.b.c", "c")],
)
def test_the_tool_tail_survives_a_workspace_renaming_its_connection(
    identity: str, tail: str
) -> None:
    assert tool_tail(identity) == tail


def test_one_recorder_dispatches_across_applications() -> None:
    """Three applications, no third recorder class.

    If this ever needs a fourth branch somewhere other than PROJECTIONS, the
    claim that an application is a template plus tools plus a page was wrong.
    """

    recorder = ToolResultArtifactRecorder(session_factory=None)  # type: ignore[arg-type]
    built = [
        recorder._build(call("ops.get_deployments", DEPLOYMENTS), RUN_ID),
        recorder._build(call("wh.query_sql", QUERY_SQL), RUN_ID),
    ]
    assert [item.type for item in built if item is not None] == [
        schemas.INCIDENT_TIMELINE,
        schemas.ANALYSIS_QUERY_RESULT,
    ]


def test_an_unrecognised_tool_is_kept_by_nobody_and_fails_nothing() -> None:
    recorder = ToolResultArtifactRecorder(session_factory=None)  # type: ignore[arg-type]
    assert recorder._build(call("calculator", {"result": 4}), RUN_ID) is None
    assert project_paper_search(call("calculator", {"result": 4}), RUN_ID) is None
    assert project_incident_timeline(call("calculator", {"result": 4}), RUN_ID) is None
    assert project_analysis_query(call("calculator", {"result": 4}), RUN_ID) is None


def test_a_result_that_is_not_structured_is_ignored() -> None:
    # Text-only results are normal for tools that were never meant to be
    # recorded, and a run must not fail because one came back.
    plain = RecordedToolCall(
        tool_identity="ops.query_logs",
        tool_call_id=str(uuid4()),
        step_sequence=1,
        data={"content": ["nothing structured here"]},
    )
    assert project_incident_timeline(plain, RUN_ID) is None
