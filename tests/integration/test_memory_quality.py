"""One small end-to-end suite asserting both positive and negative boundaries."""

import os

import pytest

from benchmarks.evaluation.memory_quality.runner import run_quality
from tests.integration.test_long_term_memory_hardening import (
    migrated_database,  # noqa: F401 -- reuse existing migration fixture
)

DATABASE_URL = os.getenv("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not DATABASE_URL, reason="PG required")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("migrated_database")
async def test_small_quality_suite_preserves_negative_results():
    result = await run_quality(DATABASE_URL)
    by_id = {o["scenario_id"]: o for o in result["observations"]}
    assert result["scenario_count"] == 11
    assert result["paid_calls"] == 0 and result["real_model"] == "NOT RUN"
    assert result["verdict"] == "NO_DEMONSTRATED_BENEFIT"
    assert len(by_id["MQ07"]["write"]) == 2
    assert {r["status"] for r in by_id["MQ07"]["write"]} == {"ACTIVE"}
    assert len(by_id["MQ07"]["admitted_ids"]) == 2
    assert by_id["MQ07"]["task_correct"] is None
    for identifier in ("MQ04", "MQ05", "MQ09"):
        assert len(by_id[identifier]["write"]) == 1  # negative semantic gate boundary
    assert by_id["MQ10"]["write"] == []  # exact quote rejection
    assert by_id["MQ11"]["write"] == []  # normalization rejection
    for identifier in ("MQ04", "MQ05", "MQ06", "MQ08", "MQ09"):
        assert len(by_id[identifier]["admitted_ids"]) == 1
        assert by_id[identifier]["use"]["scripted_consumer_ids"] == []
    hostile = by_id["MQ09"]
    assert hostile["admitted_payloads"][0]["trust"] == "UNTRUSTED"
    assert hostile["policy"]["spec_unchanged"] and hostile["policy"]["actor_unchanged"]
    assert hostile["policy"]["decision_before"] == "REQUIRE_APPROVAL"
    assert hostile["policy"]["decision_after"] == "REQUIRE_APPROVAL"
    assert hostile["policy"]["published_tool_names"] == ["create_ticket"]
    assert not hostile["policy"]["execution_attempted"]
    assert result["metrics"]["Write precision"]["numerator"] == 6
    assert result["metrics"]["Write precision"]["denominator"] == 9
    assert result["metrics"]["Forbidden/irrelevant recall"]["numerator"] == 5
    assert result["metrics"]["Forbidden/irrelevant recall"]["denominator"] == 8
    assert not result["off_sanity_control"]["write"]
    assert not result["off_sanity_control"]["admitted_ids"]
