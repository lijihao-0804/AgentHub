"""Real queue negative paths, conservative budgets and duplicate delivery."""

import asyncio
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
@pytest.mark.parametrize(
    ("marker", "limits", "calls", "turns", "stop"),
    [
        ("REFUSE", {}, 2, 2, "goal_met"),
        ("DENIED", {}, 4, 2, "goal_met"),
        ("CLARIFY", {"max_turns": 1}, 1, 1, "max_turns"),
        ("CLARIFY", {"max_tokens": 18}, 1, 1, "budget_exhausted"),
        ("UNKNOWN_USAGE", {"max_tokens": 200}, 1, 1, "budget_unavailable"),
        ("CLARIFY", {"max_cost_cny": "1.00"}, 0, 0, None),
    ],
)
async def test_queue_refusal_denied_and_limits(
    db_factory, tmp_path, marker, limits, calls, turns, stop
):
    settings = queue_settings()
    agent = await seed_queue_agent(db_factory, settings)
    api = ApiEvaluation(db_factory, settings, agent)
    audit = tmp_path / "provider.jsonl"
    worker = QueueWorker(
        settings, tmp_path / "worker", sandbox_workspaces=[agent.workspace_id], audit_path=audit
    )
    decisions = ["DENIED"] if marker == "DENIED" else []
    item = interactive_case(marker, decisions=decisions, **limits)
    try:
        await worker.start()
        frozen = await api.experiment([item])
        started = await api.start(frozen["id"])
        terminal = await api.wait_run(started["id"])
        result = (await api.results(started["id"]))[0]
        records = audit_records(audit)
        assert len(records) == calls, records
        assert all(not row["expected_label_present"] for row in records)
        if stop is None:
            assert terminal["status"] == "FAILED"
            assert result["failure_code"] == "INTERACTIVE_COST_RESERVATION_UNAVAILABLE"
        else:
            assert terminal["status"] == "SUCCEEDED", result
            assert result["observation"]["stop_reason"] == stop
            assert result["observation"]["executed_turn_count"] == turns
            assert result["observation"]["environment_lifecycle_status"] == "TEARDOWN_COMPLETE"
        if marker == "UNKNOWN_USAGE":
            assert result["input_tokens"] is None
            assert result["output_tokens"] is None
            assert result["total_tokens"] is None
            assert result["cost_amount"] is None
        if stop is not None:
            metrics = await api.post(f"/experiment-runs/{started['id']}/metrics")
            variant = next(iter(metrics["variants"].values()))
            assert variant["task_success"]["value"] == (1 if marker in {"REFUSE", "DENIED"} else 0)
        async with db_factory() as session:
            episode = await session.get(InteractiveEpisode, UUID(result["id"]))
            if stop is None:
                assert episode is None, "Finite cost rejection must precede fixture creation."
            else:
                assert episode is not None
                assert episode.assertion["passed"] is True
                assert episode.assertion["observed"]["ticket_count"] == 0
            approvals = list(
                await session.scalars(
                    select(Approval).where(Approval.workspace_id == agent.workspace_id)
                )
            )
            if marker == "DENIED":
                assert len(approvals) == 1
                assert approvals[0].decision_status == "DENIED"
                assert approvals[0].execution_status == "NOT_STARTED"
                assert "create_ticket" in result["observation"]["proposed_tool_sequence"]
                assert "create_ticket" not in result["observation"]["executed_tool_sequence"]
            else:
                assert approvals == []
            for model in (Customer, Ticket):
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.workspace_id == agent.workspace_id)
                    )
                    == 0
                )
        ordinary_logs = worker.diagnostics() + (worker.job_dir / "stdout.log").read_text(
            encoding="utf-8", errors="replace"
        )
        assert item["expected"]["canary"] not in ordinary_logs
        if episode is not None:
            assert episode.customer_ref not in ordinary_logs
        (tmp_path / "acceptance.json").write_text(
            json.dumps(
                {
                    "marker": marker,
                    "limits": limits,
                    "provider_calls": len(records),
                    "status": result["status"],
                    "stop_reason": stop,
                    "evidence_kind": "DETERMINISTIC_CONFORMANCE",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        await worker.stop()
        await api.close()


@pytest.mark.asyncio
async def test_duplicate_delivery_two_workers_has_one_episode_and_write(db_factory, tmp_path):
    settings = queue_settings()
    agent = await seed_queue_agent(db_factory, settings)
    api = ApiEvaluation(db_factory, settings, agent)
    audit = tmp_path / "provider.jsonl"
    workers = [
        QueueWorker(
            settings,
            tmp_path / f"worker-{index}",
            sandbox_workspaces=[agent.workspace_id],
            audit_path=audit,
        )
        for index in range(2)
    ]
    try:
        for worker in workers:
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
        started = await api.start(frozen["id"])
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    workers[0].celery.send_task,
                    "agenthub.execute_experiment_run",
                    args=[started["id"]],
                )
                for _ in range(3)
            )
        )
        terminal = await api.wait_run(started["id"])
        assert terminal["status"] == "SUCCEEDED"
        result = (await api.results(started["id"]))[0]
        async with db_factory() as session:
            episodes = list(
                await session.scalars(
                    select(InteractiveEpisode).where(
                        InteractiveEpisode.workspace_id == agent.workspace_id
                    )
                )
            )
            assert len(episodes) == 1
            episode = episodes[0]
            assert episode.case_id == UUID(result["id"])
            assert episode.assertion["observed"]["ticket_count"] == 1
            turns = list(
                await session.scalars(
                    select(InteractiveEpisodeTurn).where(
                        InteractiveEpisodeTurn.case_id == episode.case_id
                    )
                )
            )
            assert len(turns) == 2
            assert len(set(turn.agent_run_id for turn in turns)) == 2
            runs = list(
                await session.scalars(
                    select(AgentRun).where(AgentRun.workspace_id == agent.workspace_id)
                )
            )
            assert len(runs) == 2
            approvals = list(
                await session.scalars(
                    select(Approval).where(Approval.workspace_id == agent.workspace_id)
                )
            )
            assert len(approvals) == 1
            assert approvals[0].execution_status == "SUCCEEDED"
        assert len(audit_records(audit)) == 4
    finally:
        for worker in workers:
            await worker.stop()
        await api.close()
