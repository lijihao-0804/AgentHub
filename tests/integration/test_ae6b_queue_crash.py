"""Real worker death/restart at four persisted interactive boundaries."""

import asyncio
import json
import os
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
    case_result_observation,
    interactive_case,
    queue_settings,
    seed_queue_agent,
)

__all__ = ["db_factory", "migrated_database"]
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("point", "expected_calls", "expected_status"),
    [
        ("fixture_committed", 0, "FAILED"),
        ("first_run_terminal", 1, "FAILED"),
        ("write_response_lost", 3, "FAILED"),
        ("observation_retained", 4, "SUCCEEDED"),
    ],
)
async def test_worker_crash_recovery(db_factory, tmp_path, point, expected_calls, expected_status):
    settings = queue_settings()
    agent = await seed_queue_agent(db_factory, settings)
    api = ApiEvaluation(db_factory, settings, agent)
    audit, fault = tmp_path / "provider.jsonl", tmp_path / "fault.json"
    original = QueueWorker(
        settings,
        tmp_path / "original",
        sandbox_workspaces=[agent.workspace_id],
        audit_path=audit,
        extra_env={
            "AGENTHUB_AE6B_FAULT_POINT": point,
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
        assert fault.is_file(), original.diagnostics()
        injected = json.loads(fault.read_text(encoding="utf-8"))
        assert injected["point"] == point
        assert injected["pid"] != os.getpid()
        before = audit_records(audit)
        assert len(before) == expected_calls, before
        before_results = await api.results(started["id"])
        assert len(before_results) == 1
        assert before_results[0]["status"] == "RUNNING"
        assert before_results[0]["observation"] == {}
        case_id = UUID(before_results[0]["id"])
        assert injected["case_id"] == str(case_id)
        assert injected["workspace_id"] == str(agent.workspace_id)
        assert injected["run_id"] == started["id"]
        assert injected["pid"] == death["worker_pid"]
        async with db_factory() as session:
            persisted = await session.get(InteractiveEpisode, case_id)
            assert persisted is not None
            customer_id = persisted.customer_id
            assert await session.get(Customer, customer_id) is not None
            before_turns = list(
                await session.scalars(
                    select(InteractiveEpisodeTurn)
                    .where(InteractiveEpisodeTurn.case_id == case_id)
                    .order_by(InteractiveEpisodeTurn.turn_index)
                )
            )
            expected_turn_count = (
                0 if point == "fixture_committed" else (1 if point == "first_run_terminal" else 2)
            )
            assert len(before_turns) == expected_turn_count
            before_ticket_count = await session.scalar(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.workspace_id == agent.workspace_id)
            )
            assert before_ticket_count == (
                1 if point in {"write_response_lost", "observation_retained"} else 0
            )
            if point == "fixture_committed":
                assert persisted.prepare_stage == "FIXTURE_READY"
                assert persisted.thread_id is None
            if point in {"first_run_terminal", "write_response_lost"}:
                terminal_run = await session.get(AgentRun, before_turns[-1].agent_run_id)
                assert injected["agent_run_id"] == str(terminal_run.id)
                assert terminal_run.status == (
                    "SUCCEEDED" if point == "first_run_terminal" else "NEEDS_ATTENTION"
                )
                assert before_turns[-1].dispatch_status == "DISPATCH_INTENT"
            if point == "observation_retained":
                assert persisted.assertion["passed"] is True
                assert persisted.assertion["observed"]["ticket_count"] == 1
                assert persisted.observation_envelope is not None
        # Allow the dead owner's lease to expire, then use the actual registered
        # reconciliation task on the broker; no direct runner/reconcile call.
        await asyncio.sleep(6)
        await replacement.start()
        await asyncio.to_thread(replacement.celery.send_task, "agenthub.reconcile_evaluation_runs")
        terminal = await api.wait_run(started["id"])
        assert terminal["status"] == expected_status, terminal
        results = await api.results(started["id"])
        result = results[0]
        assert result["status"] == expected_status, result
        assert result["observation"] == {}
        stored_observation = await case_result_observation(
            db_factory,
            workspace_id=agent.workspace_id,
            run_id=started["id"],
            case_id=case_id,
        )
        assert audit_records(audit) == before, "Recovery must never replay the provider."
        async with db_factory() as session:
            episode = await session.get(InteractiveEpisode, case_id)
            assert episode is not None and episode.customer_id == customer_id
            turns = list(
                await session.scalars(
                    select(InteractiveEpisodeTurn).where(InteractiveEpisodeTurn.case_id == case_id)
                )
            )
            count = await session.scalar(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.workspace_id == agent.workspace_id)
            )
            approvals = list(
                await session.scalars(
                    select(Approval).where(Approval.workspace_id == agent.workspace_id)
                )
            )
            if point == "write_response_lost":
                assert result["failure_code"] == "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
                assert episode.cleanup_status == "CLEANUP_BLOCKED"
                assert count == 1
                assert await session.get(Customer, customer_id) is not None
                runs = list(
                    await session.scalars(
                        select(AgentRun).where(
                            AgentRun.id.in_([turn.agent_run_id for turn in turns])
                        )
                    )
                )
                assert [run for run in runs if run.status == "NEEDS_ATTENTION"]
                assert len(approvals) == 1
                assert approvals[0].decision_status == "APPROVED"
                assert approvals[0].execution_status == "UNKNOWN_OUTCOME"
            else:
                assert episode.cleanup_status == "TEARDOWN_COMPLETE"
                assert count == 0
                assert await session.get(Customer, customer_id) is None
                if point == "observation_retained":
                    assert episode.assertion["passed"] is True
                    assert episode.assertion["observed"]["ticket_count"] == 1
                else:
                    assert result["failure_code"] == "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
                    assert stored_observation["execution_complete"] is False
            proof = {
                "point": point,
                "case_id": str(case_id),
                "workspace_id": str(agent.workspace_id),
                "run_id": started["id"],
                "worker_exit_code": death["exit_code"],
                "worker_pid": death["worker_pid"],
                "worker_parent_pid": death["worker_parent_pid"],
                "launched_process_pid": death["pid"],
                "provider_calls_before": len(before),
                "provider_calls_after": len(audit_records(audit)),
                "case_status": result["status"],
                "instance_id": str(episode.instance_id),
                "agent_run_ids": [str(turn.agent_run_id) for turn in turns],
                "actions": [
                    {
                        "approval_id": str(approval.id),
                        "logical_action_id": approval.logical_action_id,
                        "decision_status": approval.decision_status,
                        "execution_status": approval.execution_status,
                    }
                    for approval in approvals
                ],
                "turn_count": len(turns),
                "cleanup_status": episode.cleanup_status,
                "assertion_source": episode.assertion_source,
                "assertion_hash": episode.assertion_hash,
                "observation_hash": episode.observation_hash,
                "ticket_count_before": before_ticket_count,
                "ticket_count_after": count,
                "evidence_kind": "DETERMINISTIC_CONFORMANCE",
            }
            (tmp_path / "acceptance.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    finally:
        await original.stop()
        await replacement.stop()
        await api.close()
