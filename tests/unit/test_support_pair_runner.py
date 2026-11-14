import asyncio
from types import SimpleNamespace

import pytest

from benchmarks.evaluation.experiments.support_refund_rag.answer_contract_pair import LimitedMeter


def test_fifth_wire_never_reaches_inner_dispatch():
    calls = []

    class Meter:
        async def acompletion(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                id=str(len(calls)),
                model="offline",
                usage=SimpleNamespace(prompt_tokens=17, completion_tokens=3),
            )

    async def exercise():
        guard = LimitedMeter(Meter())
        for _ in range(4):
            await guard.acompletion()
        with pytest.raises(RuntimeError, match="four-request"):
            await guard.acompletion()
        assert len(calls) == len(guard.receipts) == 4

    asyncio.run(exercise())
