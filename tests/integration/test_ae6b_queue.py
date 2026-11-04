"""Formal HTTP routes, Redis and owned Celery subprocess acceptance."""

import json
from uuid import UUID

import pytest
from sqlalchemy import func, select

from packages.agent_runtime.models import AgentRun
from packages.approvals.models import Approval
from packages.evaluation.models import InteractiveEpisode, InteractiveEpisodeTurn
from packages.tools.models import Customer, Ticket
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database
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


@pytest.mark.asyncio
async def test_http_queue_clarify_write_and_cleanup(db_factory, tmp_path):
    settings = queue_settings()
    agent = await seed_queue_agent(db_factory, settings)
    api = ApiEvaluation(db_factory, settings, agent)
    audit = tmp_path / "provider.jsonl"
    worker = QueueWorker(
        settings, tmp_path / "worker", sandbox_workspaces=[agent.workspace_id], audit_path=audit
    )
    try:
        await worker.start()
        frozen = await api.experiment(
            [
                interactive_case(
                    "CLARIFY",
                    assertions={"ticket_count": 1, "min_turns": 2},
                    decisions=["APPROVED"],
                )
            ]
        )
        detail = await api.client.get(api.prefix + f"/experiments/{frozen['id']}")
        assert detail.status_code == 200, detail.text
        listed = await api.client.get(api.prefix + "/experiments")
        assert listed.status_code == 200, listed.text
        projected = next(row for row in listed.json()["items"] if row["id"] == frozen["id"])
        assert projected["dataset_id"] == detail.json()["dataset_id"] == frozen["dataset_id"]
        started = await api.start(frozen["id"])
        completed = await api.wait_run(started["id"])
        assert completed["status"] == "SUCCEEDED", completed
        results = await api.results(started["id"])
        assert len(results) == 1, results
        result = results[0]
        assert result["status"] == "SUCCEEDED", result
        async with db_factory() as session:
            episode = await session.get(InteractiveEpisode, UUID(result["id"]))
            assert episode is not None
            assert episode.assertion["passed"] is True, episode.assertion
            assert episode.assertion["observed"]["ticket_count"] == 1
            assert episode.cleanup_status == "TEARDOWN_COMPLETE"
            turns = list(
                await session.scalars(
                    select(InteractiveEpisodeTurn)
                    .where(InteractiveEpisodeTurn.case_id == episode.case_id)
                    .order_by(InteractiveEpisodeTurn.turn_index)
                )
            )
            assert len(turns) == 2
            assert len({turn.input_hash for turn in turns}) == 2
            assert len({turn.agent_run_id for turn in turns}) == 2
            agent_runs = list(
                await session.scalars(
                    select(AgentRun).where(AgentRun.id.in_([turn.agent_run_id for turn in turns]))
                )
            )
            assert len(agent_runs) == 2
            assert all(
                run.agent_version_id == agent.agent_version_id and run.status == "SUCCEEDED"
                for run in agent_runs
            )
            approval = (
                await session.scalars(
                    select(Approval).where(Approval.workspace_id == agent.workspace_id)
                )
            ).one()
            assert approval.requested_by == agent.owner_id
            assert approval.decided_by == agent.approver_id
            assert approval.requested_by != approval.decided_by
            assert approval.decision_status == "APPROVED"
            assert approval.execution_status == "SUCCEEDED"
            assert approval.execution_attempt_count == 1
            for model in (Customer, Ticket):
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.workspace_id == agent.workspace_id)
                    )
                    == 0
                )
        records = audit_records(audit)
        assert len(records) == 4, records
        assert {entry["model"] for entry in records} == {"deepseek-v4.1-flash"}
        metrics = await api.post(f"/experiment-runs/{started['id']}/metrics")
        persisted = await api.client.get(api.prefix + f"/experiment-runs/{started['id']}/metrics")
        assert persisted.status_code == 200, persisted.text
        assert persisted.json()["metrics_hash"] == metrics["metrics_hash"]
        variant = next(iter(metrics["variants"].values()))
        assert variant["task_success"]["value"] == 1
        assert variant["task_success"]["sample_count"] == 1
        assert not records[0]["user_fixture_ref_present"]
        assert records[1]["user_fixture_ref_present"]
        assert all(not row["expected_label_present"] for row in records)
        assert all(not row["fixture_ref_outside_user_before_disclosure"] for row in records)
        (tmp_path / "acceptance.json").write_text(
            json.dumps(
                {
                    "scenario": "clarify_approved_write",
                    "case_id": result["id"],
                    "run_id": started["id"],
                    "workspace_id": str(agent.workspace_id),
                    "agent_version_id": str(agent.agent_version_id),
                    "agent_run_ids": [str(turn.agent_run_id) for turn in turns],
                    "approval_id": str(approval.id),
                    "logical_action_id": approval.logical_action_id,
                    "distinct_approval_actor": True,
                    "execution_attempt_count": 1,
                    "provider_calls": len(records),
                    "ticket_count_asserted": 1,
                    "cleanup_status": episode.cleanup_status,
                    "metrics_hash": metrics["metrics_hash"],
                    "task_success": variant["task_success"]["value"],
                    "evidence_kind": "DETERMINISTIC_CONFORMANCE",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        await worker.stop()
        await api.close()
