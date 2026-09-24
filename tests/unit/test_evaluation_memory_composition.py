from types import SimpleNamespace

import pytest

from apps.worker.tasks import evaluation as evaluation_tasks
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
    settings = SimpleNamespace(
        knowledge_rrf_k=60,
        knowledge_min_rerank_score=None,
        knowledge_superseded_rank_penalty=0.0,
        approval_ttl_seconds=900,
        database_url="postgresql+asyncpg://localhost/agenthub",
        credential_master_key=None,
        environment="development",
    )

    driver = await evaluation_tasks._build_driver(settings, factory=object())

    assert isinstance(driver.agent_run_service.memory_selector, SqlAlchemyMemoryStore)
    assert driver.agent_run_service.memory_writer is None
    assert driver.agent_run_service.thread_context_provider is None
