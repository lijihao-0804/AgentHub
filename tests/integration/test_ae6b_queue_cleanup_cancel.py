"""Real queue cleanup retry and cancellation closure for AE-6b."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, func, select

from packages.control_plane.models import OrganizationMembership, Workspace
from packages.core.auth.security import issue_access_token
from packages.core.canonical.json_hash import canonical_json_hash
from packages.evaluation.models import (
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    InteractiveEpisode,
)
from packages.tools.models import Customer, Ticket
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database
from tests.support.ae6b_lifecycle import fail_customer_mutation
from tests.support.ae6b_queue import (
    ApiEvaluation,
    QueueWorker,
    audit_records,
    interactive_case,
    queue_settings,
    seed_queue_agent,
)

__all__ = ["db_factory", "migrated_database"]
pytestmark = pytest.mark.integration

_RECONCILE_TASK = "agenthub.reconcile_evaluation_runs"


async def _resource_counts(session_factory, workspace_id: UUID) -> tuple[int, int]:
    async with session_factory() as session:
        customers = await session.scalar(
            select(func.count()).select_from(Customer).where(Customer.workspace_id == workspace_id)
        )
        tickets = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.workspace_id == workspace_id)
        )
    return int(customers or 0), int(tickets or 0)


async def _wait_for_cleanup_status(
    session_factory,
    *,
    case_id: UUID,
    expected: str,
    timeout: float = 90,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with session_factory() as session:
            status = await session.scalar(
                select(InteractiveEpisode.cleanup_status).where(
                    InteractiveEpisode.case_id == case_id
                )
            )
        if status == expected:
            return
        await asyncio.sleep(0.15)
    raise AssertionError(f"Episode cleanup did not reach {expected}; last status={status}")


async def _wait_for_run_as(
    client: httpx.AsyncClient,
    *,
    path: str,
    timeout: float = 90,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    last_status = "MISSING"
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(path)
        assert response.status_code == 200, response.text
        data = response.json()
        last_status = data["status"]
        if last_status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return data
        await asyncio.sleep(0.15)
    raise AssertionError(f"Experiment run did not finish; last status={last_status}")


@pytest.mark.asyncio
async def test_cleanup_failure_retries_after_terminal_case_without_replaying_provider(
    db_factory, tmp_path
):
    settings = queue_settings()
    agent = await seed_queue_agent(db_factory, settings)
    api = ApiEvaluation(db_factory, settings, agent)
    audit = tmp_path / "provider.jsonl"
    original = QueueWorker(
        settings,
        tmp_path / "original",
        sandbox_workspaces=[agent.workspace_id],
        audit_path=audit,
    )
    replacement = QueueWorker(
        settings,
        tmp_path / "replacement",
        sandbox_workspaces=[agent.workspace_id],
        audit_path=audit,
    )
    try:
        async with fail_customer_mutation(
            db_factory,
            workspace_id=agent.workspace_id,
            operation="delete",
        ):
            await original.start()
            frozen = await api.experiment(
                [
                    interactive_case(
                        "CLARIFY",
                        assertions={"ticket_count": 1, "min_turns": 2},
                        decisions=["APPROVED"],
                    )
                ]
            )
            started = await api.start(frozen["id"])
            terminal = await api.wait_run(started["id"])
            assert terminal["status"] == "SUCCEEDED", terminal

            results = await api.results(started["id"])
            assert len(results) == 1, results
            result = results[0]
            assert result["status"] == "SUCCEEDED", result
            assert result["observation"]["business_outcome"]["passed"] is True
            assert result["observation"]["business_outcome"]["observed"]["ticket_count"] == 1
            assert result["observation"]["environment_lifecycle_status"] == "TEARDOWN_FAILED"

            case_id = UUID(result["id"])
            async with db_factory() as session:
                failed = await session.get(InteractiveEpisode, case_id)
                assert failed is not None
                assert failed.cleanup_status == "TEARDOWN_FAILED"
                assert failed.cleanup_error == "INTERACTIVE_CLEANUP_FAILED"
                assert failed.cleanup_attempts >= 1
                assert failed.cleanup_next_attempt_at is not None
                assert failed.customer_id is not None
                assert failed.assertion is not None
                assert failed.assertion["passed"] is True
                assert failed.assertion["observed"]["ticket_count"] == 1
                assertion = dict(failed.assertion)
                assertion_hash = failed.assertion_hash
                assertion_source = failed.assertion_source
                observation_hash = failed.observation_hash
                customer_id = failed.customer_id
                assert assertion_hash == canonical_json_hash(assertion)
                assert assertion_source == "support_fixture.assert_state"

            assert await _resource_counts(db_factory, agent.workspace_id) == (1, 1)
            records_before = audit_records(audit)
            assert len(records_before) == 4, records_before

        # Removing the scoped trigger ends the injected storage failure. Stop the
        # original worker before asking a replacement to retry the same episode.
        await original.stop()
        async with db_factory() as session:
            failed = await session.get(InteractiveEpisode, case_id)
            assert failed is not None
            assert failed.cleanup_status == "TEARDOWN_FAILED"
            assert failed.customer_id == customer_id
            assert failed.assertion == assertion
            assert failed.assertion_hash == assertion_hash
            assert failed.assertion_source == assertion_source
            assert failed.observation_hash == observation_hash
            # This is the only failure-retry field changed by the test. The first
            # real reconcile run remains responsible for cleanup and evidence.
            failed.cleanup_next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        await replacement.start()
        await asyncio.to_thread(replacement.celery.send_task, _RECONCILE_TASK)
        await _wait_for_cleanup_status(
            db_factory,
            case_id=case_id,
            expected="TEARDOWN_COMPLETE",
        )

        assert audit_records(audit) == records_before
        assert await _resource_counts(db_factory, agent.workspace_id) == (0, 0)
        async with db_factory() as session:
            completed = await session.get(InteractiveEpisode, case_id)
            assert completed is not None
            assert completed.customer_id == customer_id
            assert completed.cleanup_status == "TEARDOWN_COMPLETE"
            assert completed.cleanup_error is None
            assert completed.cleanup_next_attempt_at is None
            assert completed.assertion == assertion
            assert completed.assertion_hash == assertion_hash
            assert completed.assertion_source == assertion_source
            assert completed.observation_hash == observation_hash
            assert await session.get(Customer, customer_id) is None

            (tmp_path / "acceptance.json").write_text(
                json.dumps(
                    {
                        "case_id": str(case_id),
                        "workspace_id": str(agent.workspace_id),
                        "provider_calls_before_retry": len(records_before),
                        "provider_calls_after_retry": len(audit_records(audit)),
                        "initial_cleanup_status": "TEARDOWN_FAILED",
                        "final_cleanup_status": completed.cleanup_status,
                        "assertion_source": completed.assertion_source,
                        "evidence_kind": "DETERMINISTIC_CONFORMANCE",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        await original.stop()
        await replacement.stop()
        await api.close()


@pytest.mark.asyncio
async def test_revoked_owner_is_rejected_before_episode_or_provider_allocation(
    db_factory, tmp_path
):
    settings = queue_settings()
    agent = await seed_queue_agent(db_factory, settings)
    assert agent.approver_id != agent.owner_id
    api = ApiEvaluation(db_factory, settings, agent)
    audit = tmp_path / "provider.jsonl"
    worker = QueueWorker(
        settings,
        tmp_path / "worker",
        sandbox_workspaces=[agent.workspace_id],
        audit_path=audit,
    )
    approver_token = issue_access_token(agent.approver_id, settings)
    approver = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app),
        base_url="http://ae6b.test",
        headers={"Authorization": "Bearer " + approver_token},
    )
    try:
        frozen = await api.experiment(
            [
                interactive_case(
                    "CLARIFY",
                    assertions={"ticket_count": 1, "min_turns": 2},
                    decisions=["APPROVED"],
                )
            ]
        )
        started = await api.start(frozen["id"])

        # The queue has no consumer yet. Remove the run creator's real organization
        # membership before a worker can resolve the execution context.
        async with db_factory() as session:
            run = await session.get(EvaluationExperimentRun, UUID(started["id"]))
            assert run is not None
            assert run.status == "QUEUED"
            assert run.created_by == agent.owner_id
            workspace = await session.get(Workspace, agent.workspace_id)
            assert workspace is not None
            removed = await session.execute(
                delete(OrganizationMembership).where(
                    OrganizationMembership.organization_id == workspace.organization_id,
                    OrganizationMembership.user_id == agent.owner_id,
                )
            )
            assert removed.rowcount == 1
            await session.commit()

        owner_response = await api.client.get(api.prefix + f"/experiment-runs/{started['id']}")
        assert owner_response.status_code in {403, 404}, owner_response.text

        await worker.start()
        # The ADMIN approver is a distinct actor from the revoked owner. Its real
        # JWT resolves organization-level read access while the owner's token cannot.
        terminal = await _wait_for_run_as(
            approver,
            path=api.prefix + f"/experiment-runs/{started['id']}",
        )
        assert terminal["status"] == "FAILED", terminal
        results_response = await approver.get(
            api.prefix + f"/experiment-runs/{started['id']}/case-results"
        )
        assert results_response.status_code == 200, results_response.text
        results = results_response.json()["items"]
        assert len(results) == 1, results
        result = results[0]
        assert result["status"] == "FAILED", result
        assert result["failure_code"] == "RESOURCE_NOT_FOUND", result

        async with db_factory() as session:
            run = await session.get(EvaluationExperimentRun, UUID(started["id"]))
            case = await session.get(EvaluationExperimentCaseResult, UUID(result["id"]))
            assert run is not None and run.status == "FAILED"
            assert case is not None and case.status == "FAILED"
            assert case.failure_code == "RESOURCE_NOT_FOUND"
            episodes = await session.scalar(
                select(func.count())
                .select_from(InteractiveEpisode)
                .where(InteractiveEpisode.workspace_id == agent.workspace_id)
            )
            assert episodes == 0

        assert await _resource_counts(db_factory, agent.workspace_id) == (0, 0)
        assert audit_records(audit) == []
        (tmp_path / "acceptance.json").write_text(
            json.dumps(
                {
                    "experiment_run_id": started["id"],
                    "workspace_id": str(agent.workspace_id),
                    "owner_id": str(agent.owner_id),
                    "reader_id": str(agent.approver_id),
                    "distinct_actor": True,
                    "owner_http_status": owner_response.status_code,
                    "provider_calls": 0,
                    "episode_count": episodes,
                    "case_status": result["status"],
                    "run_status": terminal["status"],
                    "evidence_kind": "DETERMINISTIC_CONFORMANCE",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        await worker.stop()
        await approver.aclose()
        await api.close()


@pytest.mark.asyncio
async def test_cancel_after_fixture_commit_reconciles_case_run_and_cleanup(db_factory, tmp_path):
    settings = queue_settings()
    agent = await seed_queue_agent(db_factory, settings)
    api = ApiEvaluation(db_factory, settings, agent)
    audit = tmp_path / "provider.jsonl"
    fault = tmp_path / "fault.json"
    original = QueueWorker(
        settings,
        tmp_path / "original",
        sandbox_workspaces=[agent.workspace_id],
        audit_path=audit,
        extra_env={
            "AGENTHUB_AE6B_FAULT_POINT": "fixture_committed",
            "AGENTHUB_AE6B_FAULT_PATH": str(fault),
        },
    )
    replacement = QueueWorker(
        settings,
        tmp_path / "replacement",
        sandbox_workspaces=[agent.workspace_id],
        audit_path=audit,
    )
    try:
        await original.start()
        frozen = await api.experiment(
            [
                interactive_case(
                    "CLARIFY",
                    assertions={"ticket_count": 1, "min_turns": 2},
                    decisions=["APPROVED"],
                )
            ]
        )
        started = await api.start(frozen["id"])
        death = await original.wait_exit(73, timeout=90)
        assert death["exit_code"] == 73
        assert fault.is_file(), original.diagnostics()
        injected = json.loads(fault.read_text(encoding="utf-8"))
        assert injected["point"] == "fixture_committed"
        assert injected["pid"] != os.getpid()
        assert injected["run_id"] == started["id"]
        assert audit_records(audit) == []

        before_results = await api.results(started["id"])
        assert len(before_results) == 1, before_results
        assert before_results[0]["status"] == "RUNNING", before_results[0]
        case_id = UUID(before_results[0]["id"])
        async with db_factory() as session:
            episode = await session.get(InteractiveEpisode, case_id)
            assert episode is not None
            assert episode.customer_id is not None
            reserved_thread_id = episode.reserved_thread_id
            customer_id = episode.customer_id
            assert await session.get(Customer, customer_id) is not None
        assert await _resource_counts(db_factory, agent.workspace_id) == (1, 0)

        cancelled = await api.post(f"/experiment-runs/{started['id']}/cancel")
        assert cancelled["status"] == "CANCEL_REQUESTED", cancelled

        # The dead worker uses the real five-second run lease from QueueWorker.
        await asyncio.sleep(6)
        await original.stop()
        await replacement.start()
        await asyncio.to_thread(replacement.celery.send_task, _RECONCILE_TASK)

        terminal = await api.wait_run(started["id"])
        assert terminal["status"] == "CANCELLED", terminal
        results = await api.results(started["id"])
        assert len(results) == 1, results
        result = results[0]
        assert result["status"] == "CANCELLED", result
        assert result["failure_code"] == "EVALUATION_INTERACTIVE_CANCELLED"
        await _wait_for_cleanup_status(
            db_factory,
            case_id=case_id,
            expected="TEARDOWN_COMPLETE",
        )

        assert audit_records(audit) == []
        assert await _resource_counts(db_factory, agent.workspace_id) == (0, 0)
        async with db_factory() as session:
            completed = await session.get(InteractiveEpisode, case_id)
            assert completed is not None
            assert completed.reserved_thread_id == reserved_thread_id
            assert completed.customer_id == customer_id
            assert completed.cleanup_status == "TEARDOWN_COMPLETE"
            assert completed.cleanup_error is None
            assert await session.get(Customer, customer_id) is None

            (tmp_path / "acceptance.json").write_text(
                json.dumps(
                    {
                        "case_id": str(case_id),
                        "workspace_id": str(agent.workspace_id),
                        "experiment_run_id": started["id"],
                        "worker_exit_code": death["exit_code"],
                        "fault_pid": injected["pid"],
                        "provider_calls": 0,
                        "case_status": result["status"],
                        "cleanup_status": completed.cleanup_status,
                        "evidence_kind": "DETERMINISTIC_CONFORMANCE",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        await original.stop()
        await replacement.stop()
        await api.close()
