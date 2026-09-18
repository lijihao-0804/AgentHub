from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from benchmarks.agent_runtime.runner import run_benchmark
from benchmarks.agent_runtime.schema import load_dataset
from packages.core.config.settings import get_settings

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
DATASET_PATH = Path(__file__).parents[2] / "benchmarks" / "agent_runtime" / "dataset.json"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M4-E PostgreSQL integration tests.",
    ),
]


@pytest.fixture(scope="session")
def migrated_database() -> None:
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def migrated(migrated_database: None) -> None:
    del migrated_database
    yield


@pytest.mark.asyncio
async def test_m4e_full_dataset_uses_real_runtime_and_keeps_approval_handlers_unexecuted(
    migrated,
) -> None:
    dataset = load_dataset(DATASET_PATH)
    result = await run_benchmark(dataset, TEST_DATABASE_URL)

    assert len(result.cases) == 20
    assert result.metrics()["case_pass_rate"] == 1.0
    assert result.metrics()["tool_sequence_accuracy"] == 1.0
    assert result.metrics()["terminal_status_accuracy"] == 1.0
    assert result.metrics()["failure_code_accuracy"] == 1.0
    assert all(rate == 1.0 for rate in result.category_metrics().values())
    approval_cases = [case for case in result.cases if case.category == "approval_unavailable"]
    assert len(approval_cases) == 3
    assert all(case.actual_handler_calls == 0 for case in approval_cases)
    assert all(case.actual_tool_sequence == () for case in approval_cases)
