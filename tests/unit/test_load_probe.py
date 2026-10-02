import asyncio

import pytest

from benchmarks.evaluation.load_probe import measure_phase, run_probe


@pytest.mark.asyncio
async def test_probe_counts_failures_and_unknowns_without_fabricating_zero_latency():
    async def failure():
        raise RuntimeError("private provider error body")

    report = await measure_phase(failure, concurrency=1, duration=0.01)
    assert report["attempt_count"] == 1
    assert report["failure_or_unknown_rate"] == 1
    assert report["successful_count"] == 0
    assert report["failure_codes"] == {"RuntimeError": 1}
    assert "private" not in str(report)
    assert report["latency_p95_ms"] is not None


@pytest.mark.asyncio
async def test_probe_timeout_stays_unknown():
    async def hung():
        await asyncio.sleep(1)

    report = await measure_phase(hung, concurrency=1, duration=0.01, request_timeout=0.001)
    assert report["failure_codes"] == {"PROBE_TIMEOUT": 1}
    assert report["records"][0]["status"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_probe_resource_bounds_and_database_scope_are_enforced(tmp_path):
    async def success():
        return {"status": "SUCCEEDED"}

    for options in ({"concurrency": 16, "duration": 1}, {"concurrency": 1, "duration": 61}):
        with pytest.raises(ValueError, match="RESOURCE_LIMIT"):
            await measure_phase(success, **options)
    with pytest.raises(ValueError, match="ISOLATED"):
        await run_probe(
            "postgresql+asyncpg://localhost/agenthub", duration=1, output=tmp_path / "x"
        )
