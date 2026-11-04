"""AE-10 evidence API against PostgreSQL and real tenant authorization."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from apps.api.app import create_app
from packages.agent_runtime.models import AgentRun, RunStep
from packages.approvals.models import Approval
from packages.control_plane.models import OrganizationMembership, User, WorkspaceMembership
from packages.core.auth.security import issue_access_token
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.evidence_view import EvaluationEvidenceViewService
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperimentCaseResult,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
)
from packages.evaluation.runner import ExperimentRunner
from packages.threads.models import AgentThread, ThreadTurn
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m7b_experiments import _manager_context
from tests.integration.test_m7c_experiment_runner import (
    _ready_run,
    db_factory,
    migrated_database,
)

__all__ = ["db_factory", "migrated_database"]

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run AE-10 PostgreSQL integration tests.",
    ),
]

_PRIVATE_SENTINEL = "AE10_PRIVATE_SENTINEL"


def _assert_no_private_content(value):
    """Inspect every object and array in the actual HTTP response body."""
    if isinstance(value, dict):
        assert {
            "prompt",
            "private_facts",
            "private_fact",
            "raw_arguments",
            "private_arguments",
            "expected",
            "input",
            "source_provenance",
        }.isdisjoint(value)
        for child in value.values():
            _assert_no_private_content(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_private_content(child)
    elif isinstance(value, str):
        assert _PRIVATE_SENTINEL not in value


async def _seed_evidence_case(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"ae10-evidence-{uuid4().hex}")
        run = await _ready_run(session, base, case_count=1)
        await ExperimentRunner(db_factory).prepare_run(session, run_id=run.id)
        other = await _seed(session, label=f"ae10-other-{uuid4().hex}")
        cases = list(
            await session.scalars(
                select(EvaluationExperimentCaseResult)
                .where(
                    EvaluationExperimentCaseResult.workspace_id == base["workspace_id"],
                    EvaluationExperimentCaseResult.experiment_run_id == run.id,
                )
                .order_by(
                    EvaluationExperimentCaseResult.experiment_variant_id,
                    EvaluationExperimentCaseResult.id,
                )
            )
        )
        assert len(cases) == 2
        rich_case, old_case = cases
        item = await session.get(EvaluationDatasetItem, rich_case.dataset_item_id)
        assert item is not None

        agent_run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            status="SUCCEEDED",
            input_text=_PRIVATE_SENTINEL,
            created_by=base["user"].id,
        )
        session.add(agent_run)
        await session.flush()
        thread = AgentThread(
            workspace_id=base["workspace_id"],
            agent_id=base["version"].agent_id,
            title=_PRIVATE_SENTINEL,
            kind="evaluation",
            created_by=base["user"].id,
        )
        session.add(thread)
        await session.flush()
        thread_turn = ThreadTurn(
            workspace_id=base["workspace_id"],
            thread_id=thread.id,
            sequence=0,
            user_input=_PRIVATE_SENTINEL,
            agent_run_id=agent_run.id,
            client_token=f"ae10-{uuid4().hex}",
            input_hash="1" * 64,
        )
        session.add(thread_turn)
        await session.flush()
        agent_run.thread_id = thread.id
        agent_run.thread_turn_id = thread_turn.id
        action_succeeded = Approval(
            workspace_id=base["workspace_id"],
            run_id=agent_run.id,
            agent_version_id=base["version"].id,
            logical_action_id=f"logical-success-{uuid4().hex}",
            tool_revision_id=None,
            tool_identity="create_ticket",
            canonical_arguments={"private": _PRIVATE_SENTINEL},
            canonical_args_hash="b" * 64,
            decision_status="APPROVED",
            execution_status="SUCCEEDED",
            requested_by=base["user"].id,
            idempotency_key=f"ae10-{uuid4().hex}",
        )
        action_failed = Approval(
            workspace_id=base["workspace_id"],
            run_id=agent_run.id,
            agent_version_id=base["version"].id,
            logical_action_id=f"logical-failed-{uuid4().hex}",
            tool_revision_id=None,
            tool_identity="send_email",
            canonical_arguments={"private": _PRIVATE_SENTINEL},
            canonical_args_hash="c" * 64,
            decision_status="APPROVED",
            execution_status="FAILED",
            failure_code="EMAIL_PROVIDER_REJECTED",
            requested_by=base["user"].id,
            idempotency_key=f"ae10-{uuid4().hex}",
        )
        action_unknown = Approval(
            workspace_id=base["workspace_id"],
            run_id=agent_run.id,
            agent_version_id=base["version"].id,
            logical_action_id=f"logical-unknown-{uuid4().hex}",
            tool_revision_id=None,
            tool_identity="update_ticket",
            canonical_arguments={"private": _PRIVATE_SENTINEL},
            canonical_args_hash="8" * 64,
            decision_status="APPROVED",
            execution_status="UNKNOWN_OUTCOME",
            failure_code="WRITE_RESPONSE_LOST",
            requested_by=base["user"].id,
            idempotency_key=f"ae10-{uuid4().hex}",
        )
        action_denied = Approval(
            workspace_id=base["workspace_id"],
            run_id=agent_run.id,
            agent_version_id=base["version"].id,
            logical_action_id=f"logical-denied-{uuid4().hex}",
            tool_revision_id=None,
            tool_identity="delete_account",
            canonical_arguments={"private": _PRIVATE_SENTINEL},
            canonical_args_hash="d" * 64,
            decision_status="DENIED",
            execution_status="NOT_STARTED",
            requested_by=base["user"].id,
            idempotency_key=f"ae10-{uuid4().hex}",
        )
        session.add_all([action_succeeded, action_failed, action_unknown, action_denied])
        await session.flush()

        expected_chunk_ids = list(item.expected["relevant_chunk_ids"])
        rich_case.agent_run_id = agent_run.id
        rich_case.status = "SUCCEEDED"
        rich_case.observation = {
            "category": "RETRIEVAL",
            "candidate_chunk_ids": expected_chunk_ids,
            "final_chunk_ids": expected_chunk_ids,
            "citation_ids": expected_chunk_ids,
            "candidate_top_k": 20,
            "final_top_k": 5,
            "environment_lifecycle_status": "TEARDOWN_COMPLETE",
            "termination_reason": "HISTORICAL_TERMINATION",
            "trajectory_version": "trajectory-v1",
            "trajectory_steps": [
                {"index": 0, "sequence_number": 1},
                {"index": 1, "sequence_number": 2},
                {"index": 2, "sequence_number": 3},
                {"index": 3, "sequence_number": 4},
                {"index": 4, "sequence_number": 5},
                {"index": 5, "sequence_number": 6},
                {"index": 6, "sequence_number": 7},
                {"index": 7, "sequence_number": 8},
            ],
            "failure_taxonomy": [
                {"code": "UNKNOWN_WRITE_OUTCOME", "step_index": 7},
            ],
            "prompt": _PRIVATE_SENTINEL,
            "private_facts": {"secret": _PRIVATE_SENTINEL},
            "raw_arguments": {"private": _PRIVATE_SENTINEL},
        }
        old_case.observation = {
            "category": "RETRIEVAL",
            "candidate_chunk_ids": [],
            "final_chunk_ids": [],
            "citation_ids": [],
            "candidate_top_k": 20,
            "final_top_k": 5,
            "prompt": _PRIVATE_SENTINEL,
        }

        terminal_at = datetime.now(UTC)
        terminal_evidence = {
            "evidence_source": "evaluation_fixture",
            "run_id": str(agent_run.id),
            "status": "SUCCEEDED",
        }
        episode = InteractiveEpisode(
            case_id=rich_case.id,
            workspace_id=base["workspace_id"],
            instance_id=rich_case.id,
            reserved_thread_id=thread.id,
            thread_id=thread.id,
            seed=0,
            fixture_ref="support_customer",
            fixture_hash="2" * 64,
            env_kind="support_fixture",
            env_version="environment-v2",
            simulator_version="scripted-disclosure-v1",
            driver_version="interactive-driver-v2",
            input_hash="3" * 64,
            policy_hash="4" * 64,
            prepare_stage="PREPARED",
            execution_status="COMPLETED",
            termination_reason="UNKNOWN_OUTCOME",
            cleanup_status="CLEANUP_BLOCKED",
            cleanup_error="INTERACTIVE_CLEANUP_UNKNOWN_OUTCOME",
            completed_at=terminal_at,
        )
        session.add(episode)
        await session.flush()
        session.add(
            InteractiveEpisodeTurn(
                workspace_id=base["workspace_id"],
                case_id=rich_case.id,
                turn_index=0,
                client_token=f"ae10-{uuid4().hex}",
                input_hash="5" * 64,
                thread_turn_id=thread_turn.id,
                agent_run_id=agent_run.id,
                dispatch_status="TERMINAL",
                dispatch_intent_at=terminal_at,
                terminal_status="SUCCEEDED",
                evidence=terminal_evidence,
                evidence_hash=canonical_json_hash(terminal_evidence),
                completed_at=terminal_at,
            )
        )
        session.add_all(
            [
                RunStep(
                    workspace_id=base["workspace_id"],
                    agent_run_id=agent_run.id,
                    sequence_number=1,
                    kind="TOOL_PROPOSAL",
                    status="SUCCEEDED",
                    safe_metadata={
                        "tool_call_ids": ["read-success", "read-failed"],
                        "tool_identities": ["query_customer", "lookup_order"],
                        "arguments_hashes": ["6" * 64, "a" * 64],
                        "private_fact": _PRIVATE_SENTINEL,
                    },
                ),
                RunStep(
                    workspace_id=base["workspace_id"],
                    agent_run_id=agent_run.id,
                    sequence_number=2,
                    kind="TOOL_EXECUTE",
                    status="SUCCEEDED",
                    safe_metadata={
                        "tool_call_ids": ["read-success", "read-failed"],
                        "tool_identities": ["query_customer", "lookup_order"],
                        "tool_results": [
                            {
                                "tool_call_id": "read-success",
                                "tool_identity": "query_customer",
                                "status": "SUCCESS",
                                "error_code": None,
                            },
                            {
                                "tool_call_id": "read-failed",
                                "tool_identity": "lookup_order",
                                "status": "FAILED",
                                "error_code": "ORDER_SERVICE_TIMEOUT",
                            },
                        ],
                        "private_fact": _PRIVATE_SENTINEL,
                    },
                ),
                RunStep(
                    workspace_id=base["workspace_id"],
                    agent_run_id=agent_run.id,
                    sequence_number=3,
                    kind="APPROVAL_WAIT",
                    status="WAITING",
                    safe_metadata={
                        "tool_identities": [action_succeeded.tool_identity],
                        "policy_decision": "REQUIRE_APPROVAL",
                        "private_fact": _PRIVATE_SENTINEL,
                    },
                ),
                RunStep(
                    workspace_id=base["workspace_id"],
                    agent_run_id=agent_run.id,
                    sequence_number=4,
                    kind="APPROVAL_WAIT",
                    status="WAITING",
                    safe_metadata={
                        "tool_identities": [action_failed.tool_identity],
                        "policy_decision": "REQUIRE_APPROVAL",
                        "private_fact": _PRIVATE_SENTINEL,
                    },
                ),
                RunStep(
                    workspace_id=base["workspace_id"],
                    agent_run_id=agent_run.id,
                    sequence_number=5,
                    kind="ACTION",
                    status="FAILED",
                    safe_metadata={
                        "tool_identities": ["create_ticket", "send_email"],
                        "logical_action_ids": [
                            action_succeeded.logical_action_id,
                            action_failed.logical_action_id,
                        ],
                        "action_results": [
                            {
                                "logical_action_id": action_succeeded.logical_action_id,
                                "tool_identity": action_succeeded.tool_identity,
                                "status": "SUCCEEDED",
                                "error_code": None,
                            },
                            {
                                "logical_action_id": action_failed.logical_action_id,
                                "tool_identity": action_failed.tool_identity,
                                "status": "FAILED",
                                "error_code": action_failed.failure_code,
                            },
                        ],
                        "private_arguments": {"body": _PRIVATE_SENTINEL},
                    },
                ),
                RunStep(
                    workspace_id=base["workspace_id"],
                    agent_run_id=agent_run.id,
                    sequence_number=9,
                    kind="APPROVAL_WAIT",
                    status="WAITING",
                    safe_metadata={
                        "tool_identities": [action_unknown.tool_identity],
                        "policy_decision": "REQUIRE_APPROVAL",
                        "private_fact": _PRIVATE_SENTINEL,
                    },
                ),
                RunStep(
                    workspace_id=base["workspace_id"],
                    agent_run_id=agent_run.id,
                    sequence_number=7,
                    kind="APPROVAL_WAIT",
                    status="WAITING",
                    safe_metadata={
                        "tool_identities": [action_denied.tool_identity],
                        "policy_decision": "REQUIRE_APPROVAL",
                        "private_fact": _PRIVATE_SENTINEL,
                    },
                ),
                RunStep(
                    workspace_id=base["workspace_id"],
                    agent_run_id=agent_run.id,
                    sequence_number=8,
                    kind="GUARD",
                    status="FAILED",
                    safe_metadata={
                        "error_code": "UNKNOWN_WRITE_OUTCOME",
                        "private_fact": _PRIVATE_SENTINEL,
                    },
                ),
                RunStep(
                    workspace_id=base["workspace_id"],
                    agent_run_id=agent_run.id,
                    sequence_number=10,
                    kind="APPROVAL_WAIT",
                    status="WAITING",
                    safe_metadata={
                        "tool_identities": [action_unknown.tool_identity],
                        "policy_decision": "REQUIRE_APPROVAL",
                        "private_fact": _PRIVATE_SENTINEL,
                    },
                ),
            ]
        )

        viewer = User(
            email=f"ae10-viewer-{uuid4().hex}@example.test",
            normalized_email=f"ae10-viewer-{uuid4().hex}@example.test",
            password_hash="not-used",
        )
        session.add(viewer)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=base["organization_id"],
                user_id=viewer.id,
                role="MEMBER",
            )
        )
        session.add(
            WorkspaceMembership(
                workspace_id=base["workspace_id"],
                user_id=viewer.id,
                role="VIEWER",
            )
        )
        await session.commit()
        return {
            "base": base,
            "run_id": run.id,
            "rich_case_id": rich_case.id,
            "old_case_id": old_case.id,
            "viewer_id": viewer.id,
            "other_base": other,
            "other_user_id": other["user"].id,
        }


def _client(app, user_id: UUID, settings: Settings) -> httpx.AsyncClient:
    token = issue_access_token(user_id, settings)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ae10.test",
        headers={"Authorization": "Bearer " + token},
    )


@pytest.mark.asyncio
async def test_evidence_route_returns_safe_real_pg_projection(db_factory) -> None:
    seeded = await _seed_evidence_case(db_factory)
    base = seeded["base"]
    settings = Settings(testing=True)
    app = create_app(settings)
    app.state.db_session_factory = db_factory
    prefix = f"/api/v1/workspaces/{base['workspace_id']}/evaluation"
    owner_client = _client(app, base["user"].id, settings)
    viewer_client = _client(app, seeded["viewer_id"], settings)
    other_client = _client(app, seeded["other_user_id"], settings)
    try:
        rich_response = await owner_client.get(
            prefix
            + f"/experiment-runs/{seeded['run_id']}"
            + f"/case-results/{seeded['rich_case_id']}/evidence"
        )
        old_response = await owner_client.get(
            prefix
            + f"/experiment-runs/{seeded['run_id']}"
            + f"/case-results/{seeded['old_case_id']}/evidence"
        )
        owner_list_response = await owner_client.get(
            prefix + f"/experiment-runs/{seeded['run_id']}/case-results"
        )
        viewer_response = await viewer_client.get(
            prefix
            + f"/experiment-runs/{seeded['run_id']}"
            + f"/case-results/{seeded['rich_case_id']}/evidence"
        )
        viewer_list_response = await viewer_client.get(
            prefix + f"/experiment-runs/{seeded['run_id']}/case-results"
        )
        paged_list_responses = [
            await client.get(
                prefix + f"/experiment-runs/{seeded['run_id']}/case-results",
                params={"limit": 1, "offset": offset},
            )
            for client in (owner_client, viewer_client)
            for offset in (0, 1)
        ]
        cross_tenant_response = await other_client.get(
            prefix
            + f"/experiment-runs/{seeded['run_id']}"
            + f"/case-results/{seeded['rich_case_id']}/evidence"
        )
        cross_tenant_list_response = await other_client.get(
            prefix + f"/experiment-runs/{seeded['run_id']}/case-results"
        )
    finally:
        await owner_client.aclose()
        await viewer_client.aclose()
        await other_client.aclose()

    assert rich_response.status_code == 200, rich_response.text
    evidence = rich_response.json()
    assert evidence["schema_version"] == 1
    assert evidence["task_success"] == {
        "status": "AVAILABLE",
        "value": 1,
        "reason": None,
    }
    assert evidence["execution_cleanup_status"] == "TEARDOWN_COMPLETE"
    assert evidence["termination_reason"] == "HISTORICAL_TERMINATION"
    assert evidence["episode"]["cleanup_status"] == "CLEANUP_BLOCKED"
    assert evidence["episode"]["termination_reason"] == "UNKNOWN_OUTCOME"
    assert evidence["episode"]["turns"] == [
        {
            "turn_index": 0,
            "agent_run_id": evidence["steps"][0]["agent_run_id"],
            "status": "SUCCEEDED",
        }
    ]
    assert [step["step_order"] for step in evidence["steps"]] == [
        1,
        1,
        2,
        2,
        3,
        4,
        5,
        5,
        7,
        8,
        9,
        10,
        0,
    ]
    proposals = [step for step in evidence["steps"] if step["step_type"] == "TOOL_PROPOSAL"]
    assert [(step["tool_name"], step["status"]) for step in proposals] == [
        ("query_customer", None),
        ("lookup_order", None),
    ]
    read_calls = [step for step in evidence["steps"] if step["step_type"] == "TOOL_EXECUTE"]
    assert [(step["tool_name"], step["status"]) for step in read_calls] == [
        ("query_customer", "SUCCESS"),
        ("lookup_order", "FAILED"),
    ]
    assert [step["arguments_hash"] for step in read_calls] == ["6" * 64, "a" * 64]
    assert read_calls[1]["error_code"] == "ORDER_SERVICE_TIMEOUT"

    action_calls = [step for step in evidence["steps"] if step["step_type"] == "ACTION"]
    actions_by_tool = {step["tool_name"]: step for step in action_calls}
    assert actions_by_tool["create_ticket"]["status"] == "SUCCEEDED"
    assert actions_by_tool["create_ticket"]["arguments_hash"] == "b" * 64
    assert actions_by_tool["send_email"]["status"] == "FAILED"
    assert actions_by_tool["send_email"]["arguments_hash"] == "c" * 64
    assert set(actions_by_tool) == {"create_ticket", "send_email"}

    approval_executions = [
        step for step in evidence["steps"] if step["step_type"] == "APPROVAL_EXECUTION"
    ]
    assert len(approval_executions) == 1
    assert approval_executions[0]["step_order"] == 0
    assert approval_executions[0]["agent_run_id"] == evidence["steps"][0]["agent_run_id"]
    assert approval_executions[0]["tool_name"] == "update_ticket"
    assert approval_executions[0]["status"] == "UNKNOWN_OUTCOME"
    assert approval_executions[0]["arguments_hash"] == "8" * 64
    assert approval_executions[0]["error_code"] == "WRITE_RESPONSE_LOST"
    assert "delete_account" not in {step["tool_name"] for step in approval_executions}

    waits_by_tool = {
        step["tool_name"]: step
        for step in evidence["steps"]
        if step["step_type"] == "APPROVAL_WAIT"
    }
    assert waits_by_tool["create_ticket"]["status"] == "APPROVED"
    assert waits_by_tool["send_email"]["status"] == "APPROVED"
    assert waits_by_tool["delete_account"]["status"] == "DENIED"
    assert waits_by_tool["delete_account"]["arguments_hash"] == "d" * 64
    update_waits = [
        step
        for step in evidence["steps"]
        if step["step_type"] == "APPROVAL_WAIT" and step["tool_name"] == "update_ticket"
    ]
    assert [step["step_order"] for step in update_waits] == [9, 10]
    assert all(step["status"] == "WAITING" for step in update_waits)
    assert all(step["logical_action_id"] is None for step in update_waits)
    assert all(step["arguments_hash"] is None for step in update_waits)

    assert evidence["failure_labels"] == [{"code": "UNKNOWN_WRITE_OUTCOME", "step_orders": [8]}]
    assert _PRIVATE_SENTINEL not in rich_response.text

    assert old_response.status_code == 200, old_response.text
    old = old_response.json()
    assert old["episode"] is None
    assert old["steps"] == []
    assert old["failure_labels"] == []
    assert _PRIVATE_SENTINEL not in old_response.text

    assert owner_list_response.status_code == 200, owner_list_response.text
    owner_list = owner_list_response.json()
    assert owner_list["total"] == 2
    cases_by_id = {item["id"]: item for item in owner_list["items"]}
    assert set(cases_by_id) == {
        str(seeded["rich_case_id"]),
        str(seeded["old_case_id"]),
    }
    rich_summary = cases_by_id[str(seeded["rich_case_id"])]
    old_summary = cases_by_id[str(seeded["old_case_id"])]
    assert rich_summary["case_key"] == "case-0"
    assert rich_summary["category"] == "RETRIEVAL"
    assert rich_summary["variant_label"] in {"variant-0", "variant-1"}
    assert rich_summary["task_success"] == {
        "status": "AVAILABLE",
        "value": 1,
        "reason": None,
    }
    assert old_summary["case_key"] == "case-0"
    assert old_summary["category"] == "RETRIEVAL"
    assert old_summary["variant_label"] in {"variant-0", "variant-1"}
    assert old_summary["task_success"] == {
        "status": "AVAILABLE",
        "value": 0,
        "reason": None,
    }
    assert {item["variant_label"] for item in owner_list["items"]} == {"variant-0", "variant-1"}
    assert all(
        set(item["task_success"]) == {"status", "value", "reason"} for item in owner_list["items"]
    )
    assert all(
        {
            "expected",
            "input",
            "source_provenance",
            "raw_arguments",
            "private_facts",
        }.isdisjoint(item)
        for item in owner_list["items"]
    )

    assert viewer_response.status_code == 200, viewer_response.text
    assert viewer_list_response.status_code == 200, viewer_list_response.text
    assert viewer_list_response.json() == owner_list
    for response in (owner_list_response, viewer_list_response, *paged_list_responses):
        assert response.status_code == 200, response.text
        payload = response.json()
        _assert_no_private_content(payload)
        assert _PRIVATE_SENTINEL not in response.text
        assert all(item["observation"] == {} for item in payload["items"])
    for first, second in (paged_list_responses[:2], paged_list_responses[2:]):
        assert first.json()["total"] == second.json()["total"] == 2
        assert len(first.json()["items"]) == len(second.json()["items"]) == 1
        assert {first.json()["items"][0]["id"], second.json()["items"][0]["id"]} == set(cases_by_id)
    assert cross_tenant_response.status_code in {403, 404}, cross_tenant_response.text
    assert cross_tenant_list_response.status_code in {403, 404}, cross_tenant_list_response.text


@pytest.mark.asyncio
async def test_evidence_service_enforces_evaluation_read_and_workspace_scope(db_factory) -> None:
    seeded = await _seed_evidence_case(db_factory)
    base = seeded["base"]
    manager_context = _manager_context(base)
    no_evaluation_read = manager_context.model_copy(
        update={"permissions": frozenset({"workspace_read"})}
    )
    service = EvaluationEvidenceViewService()

    async with db_factory() as session:
        with pytest.raises(AgentHubError) as denied:
            await service.get_case_evidence(
                session,
                context=no_evaluation_read,
                run_id=seeded["run_id"],
                case_id=seeded["rich_case_id"],
            )
        assert denied.value.status_code == 403
        with pytest.raises(AgentHubError) as summary_denied:
            await service.summarize_case_results(
                session,
                context=no_evaluation_read,
                run_id=seeded["run_id"],
                cases=[],
            )
        assert summary_denied.value.status_code == 403

    async with db_factory() as session:
        with pytest.raises(AgentHubError) as missing:
            await service.get_case_evidence(
                session,
                context=_manager_context(seeded["other_base"]),
                run_id=seeded["run_id"],
                case_id=seeded["rich_case_id"],
            )
    assert missing.value.code == "EVALUATION_EXPERIMENT_RUN_NOT_FOUND"
