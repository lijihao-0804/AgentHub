"""Real API, Redis, and worker coverage for per-item evaluation routing."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperimentCaseResult,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
)
from packages.tools.models import Customer, Ticket
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database
from tests.support.ae6b_provider import (
    MARKER_PLAIN,
    PLAIN_RESPONSE,
)
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


def _plain_case(*, ordinal: int) -> dict[str, object]:
    case_key = "plain-" + uuid4().hex
    return {
        "case_key": case_key,
        "category": "KNOWLEDGE_QA",
        "split": "DEV",
        "ordinal": ordinal,
        "input": {"question": f"{MARKER_PLAIN} What is the controlled response?"},
        "expected": {"answer": PLAIN_RESPONSE, "citations": []},
        "tags": ["ae6b", "controlled-provider"],
        "source_provenance": {"source_kind": "synthetic_fixture", "source_id": case_key},
    }


def _result_items(payload) -> list[dict]:
    if isinstance(payload, dict):
        items = payload["items"]
        assert payload["total"] == len(items)
        return items
    assert isinstance(payload, list)
    return payload


async def _case_categories(factory, results: list[dict]) -> dict[UUID, str]:
    item_ids = [UUID(str(result["dataset_item_id"])) for result in results]
    async with factory() as session:
        items = list(
            await session.scalars(
                select(EvaluationDatasetItem).where(EvaluationDatasetItem.id.in_(item_ids))
            )
        )
    assert len(items) == len(set(item_ids))
    return {item.id: item.category for item in items}


@pytest.mark.asyncio
async def test_queue_routes_plain_and_interactive_items_independently(db_factory, tmp_path):
    settings = queue_settings()
    agent = await seed_queue_agent(db_factory, settings)
    api = ApiEvaluation(db_factory, settings, agent)
    audit = tmp_path / "provider.jsonl"
    worker = QueueWorker(
        settings,
        tmp_path / "worker",
        sandbox_workspaces=[agent.workspace_id],
        audit_path=audit,
    )
    try:
        await worker.start()
        frozen = await api.experiment(
            [
                _plain_case(ordinal=0),
                interactive_case(
                    "REFUSE",
                    ordinal=1,
                    assertions={"ticket_count": 0, "min_turns": 2},
                ),
            ]
        )
        started = await api.start(frozen["id"])
        completed = await api.wait_run(started["id"], timeout=180)

        assert completed["status"] == "SUCCEEDED", completed
        results = _result_items(await api.results(started["id"]))
        assert len(results) == 2
        assert {result["status"] for result in results} == {"SUCCEEDED"}
        categories = await _case_categories(db_factory, results)
        assert set(categories.values()) == {"KNOWLEDGE_QA", "INTERACTIVE"}

        interactive_case_id = next(
            UUID(str(result["id"]))
            for result in results
            if categories[UUID(str(result["dataset_item_id"]))] == "INTERACTIVE"
        )
        async with db_factory() as session:
            episode = await session.get(InteractiveEpisode, interactive_case_id)
            assert episode is not None
            assert episode.workspace_id == agent.workspace_id
            assert episode.cleanup_status == "TEARDOWN_COMPLETE"
            assert episode.assertion["passed"] is True
        assert len(audit_records(audit)) == 3
    finally:
        await worker.stop()
        await api.close()


@pytest.mark.asyncio
async def test_queue_rejects_interactive_case_without_assigned_sandbox_before_provider(
    db_factory,
    tmp_path,
):
    settings = queue_settings()
    agent = await seed_queue_agent(db_factory, settings)
    api = ApiEvaluation(db_factory, settings, agent)
    audit = tmp_path / "provider.jsonl"
    worker = QueueWorker(
        settings,
        tmp_path / "worker",
        sandbox_workspaces=[],
        audit_path=audit,
    )
    try:
        await worker.start()
        frozen = await api.experiment([interactive_case("REFUSE")])
        started = await api.start(frozen["id"])
        completed = await api.wait_run(started["id"], timeout=90)

        assert completed["status"] == "FAILED", completed
        results = _result_items(await api.results(started["id"]))
        assert len(results) == 1
        assert results[0]["status"] == "FAILED"
        assert results[0]["failure_code"] == "EVALUATION_SANDBOX_REQUIRED"
        assert audit_records(audit) == []

        async with db_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(InteractiveEpisode)
                    .where(InteractiveEpisode.workspace_id == agent.workspace_id)
                )
                == 0
            )
            for model in (Customer, Ticket):
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.workspace_id == agent.workspace_id)
                    )
                    == 0
                )
    finally:
        await worker.stop()
        await api.close()


@pytest.mark.asyncio
async def test_two_workspaces_two_cases_two_repetitions_remain_isolated(db_factory, tmp_path):
    settings = queue_settings()
    first_agent, second_agent = (
        await seed_queue_agent(db_factory, settings),
        await seed_queue_agent(db_factory, settings),
    )
    first_api = ApiEvaluation(db_factory, settings, first_agent)
    second_api = ApiEvaluation(db_factory, settings, second_agent)
    audit = tmp_path / "provider.jsonl"
    worker = QueueWorker(
        settings,
        tmp_path / "worker",
        sandbox_workspaces=[first_agent.workspace_id, second_agent.workspace_id],
        audit_path=audit,
    )
    try:
        await worker.start()
        first_frozen = await first_api.experiment(
            [
                interactive_case("REFUSE", ordinal=0),
                interactive_case("REFUSE", ordinal=1),
            ],
            repetitions=2,
        )
        second_frozen = await second_api.experiment(
            [
                interactive_case("REFUSE", ordinal=0),
                interactive_case("REFUSE", ordinal=1),
            ],
            repetitions=2,
        )
        first_started, second_started = await asyncio.gather(
            first_api.start(first_frozen["id"]),
            second_api.start(second_frozen["id"]),
        )
        first_completed, second_completed = await asyncio.gather(
            first_api.wait_run(first_started["id"], timeout=240),
            second_api.wait_run(second_started["id"], timeout=240),
        )
        assert first_completed["status"] == "SUCCEEDED", first_completed
        assert second_completed["status"] == "SUCCEEDED", second_completed

        first_results = _result_items(await first_api.results(first_started["id"]))
        second_results = _result_items(await second_api.results(second_started["id"]))
        assert len(first_results) == 4
        assert len(second_results) == 4
        assert {result["status"] for result in first_results + second_results} == {"SUCCEEDED"}
        for results in (first_results, second_results):
            assert {result["repetition_index"] for result in results} == {0, 1}
            assert len({result["dataset_item_id"] for result in results}) == 2
        assert len(audit_records(audit)) == 16

        foreign_results = await first_api.client.get(
            first_api.prefix + f"/experiment-runs/{second_started['id']}/case-results"
        )
        assert foreign_results.status_code in {403, 404}, foreign_results.text
        assert set(foreign_results.json()["error"]) == {"code", "message", "request_id"}
        second_after_denial = _result_items(await second_api.results(second_started["id"]))
        assert second_after_denial == second_results

        run_ids = (UUID(str(first_started["id"])), UUID(str(second_started["id"])))
        async with db_factory() as session:
            cases = list(
                await session.scalars(
                    select(EvaluationExperimentCaseResult).where(
                        EvaluationExperimentCaseResult.experiment_run_id.in_(run_ids)
                    )
                )
            )
            case_ids = [case.id for case in cases]
            episodes = list(
                await session.scalars(
                    select(InteractiveEpisode).where(InteractiveEpisode.case_id.in_(case_ids))
                )
            )
            turns = list(
                await session.scalars(
                    select(InteractiveEpisodeTurn).where(
                        InteractiveEpisodeTurn.case_id.in_(case_ids)
                    )
                )
            )

        assert len(cases) == 8
        assert {case.workspace_id for case in cases} == {
            first_agent.workspace_id,
            second_agent.workspace_id,
        }
        assert sum(case.workspace_id == first_agent.workspace_id for case in cases) == 4
        assert sum(case.workspace_id == second_agent.workspace_id for case in cases) == 4
        assert len(episodes) == 8
        assert len({episode.case_id for episode in episodes}) == 8
        assert len({episode.instance_id for episode in episodes}) == 8
        assert len({episode.reserved_thread_id for episode in episodes}) == 8
        assert len({episode.customer_ref for episode in episodes}) == 8
        assert all(episode.instance_id == episode.case_id for episode in episodes)
        assert all(episode.thread_id == episode.reserved_thread_id for episode in episodes)
        assert all(episode.cleanup_status == "TEARDOWN_COMPLETE" for episode in episodes)
        assert all(episode.execution_status == "COMPLETED" for episode in episodes)
        assert all(episode.assertion["passed"] is True for episode in episodes)
        assert all(
            episode.customer_ref == "AE6B-" + episode.instance_id.hex for episode in episodes
        )

        turns_by_case: dict[UUID, list[InteractiveEpisodeTurn]] = {}
        for turn in turns:
            turns_by_case.setdefault(turn.case_id, []).append(turn)
        assert set(turns_by_case) == set(case_ids)
        assert all(len(case_turns) == 2 for case_turns in turns_by_case.values())
        for episode in episodes:
            for turn in turns_by_case[episode.case_id]:
                assert turn.workspace_id == episode.workspace_id
                assert turn.client_token == f"{episode.case_id.hex}:{turn.turn_index}"
                assert turn.dispatch_status == "TERMINAL"
        assert len({turn.agent_run_id for turn in turns}) == len(turns)
        assert len({turn.thread_turn_id for turn in turns}) == len(turns)

        async with db_factory() as session:
            for workspace_id in (first_agent.workspace_id, second_agent.workspace_id):
                for model in (Customer, Ticket):
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(model)
                            .where(model.workspace_id == workspace_id)
                        )
                        == 0
                    )
    finally:
        await worker.stop()
        await first_api.close()
        await second_api.close()
