from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from benchmarks.evaluation.runner import run_formal_chain
from packages.core.config.settings import get_settings

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M7-H PostgreSQL integration tests.",
    ),
]


@pytest.fixture(scope="module")
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


@pytest.mark.asyncio
async def test_m7h_full_unified_deterministic_acceptance(migrated_database: None) -> None:
    result = await run_formal_chain(TEST_DATABASE_URL)

    assert result["status"] == "PASS"
    assert result["deterministic_conformance_only"] is True
    assert result["real_provider_eval"] == "NOT_RUN"
    assert result["dataset"]["case_count"] == 100
    assert result["dataset"]["split_counts"] == {"DEV": 70, "HOLDOUT": 30}
    assert len(result["dataset"]["category_counts"]) == 7

    dev = result["formal_pipeline"]["dev"]
    holdout = result["formal_pipeline"]["holdout"]
    assert dev["execution_count"] == 140
    assert dev["comparison_status"] == "COMPLETE"
    assert dev["complete_pairs"] == 70
    assert dev["incomplete_pairs"] == 0
    assert dev["ablation_factor"] == "PROMPT"
    assert holdout["execution_count"] == 60
    assert holdout["comparison_status"] == "COMPLETE"
    assert holdout["complete_pairs"] == 30
    assert holdout["incomplete_pairs"] == 0
    assert holdout["ablation_factor"] == "PROMPT"
    assert result["formal_pipeline"]["holdout_exposure_count"] == 1
    assert result["formal_pipeline"]["release_gate"]["status"] == "PASS"
