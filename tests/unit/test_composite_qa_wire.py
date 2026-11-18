import pytest

from benchmarks.retrieval.experiments.composite_qa.wire_check import FAILURES, scenario


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", FAILURES)
async def test_paid_meter_stops_once_at_real_transport_boundary(failure, tmp_path):
    await scenario(failure, tmp_path)
