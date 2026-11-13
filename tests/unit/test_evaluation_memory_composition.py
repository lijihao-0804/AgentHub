import pytest

from apps.worker.tasks import evaluation as evaluation_tasks
from packages.core.config.settings import Settings
from packages.evaluation.driver_router import EvaluationDriverRouter
from packages.memory.store import SqlAlchemyMemoryStore


@pytest.mark.asyncio
async def test_evaluation_runtime_reads_memory_without_learning_from_its_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evaluation_tasks, "production_retrieval_components", lambda _settings: object()
    )
    monkeypatch.setattr(
        evaluation_tasks, "LangGraphCheckpointAdapter", lambda _database_url: object()
    )
    settings = Settings(environment="test", process_role="worker", _env_file=None)

    driver = await evaluation_tasks._build_driver(settings, factory=object())

    assert isinstance(driver, EvaluationDriverRouter)
    runtime_driver = driver.runtime_driver
    assert isinstance(runtime_driver.agent_run_service.memory_selector, SqlAlchemyMemoryStore)
    assert runtime_driver.agent_run_service.memory_writer is None
    assert runtime_driver.agent_run_service.thread_context_provider is None
