"""Serial summary coordinator safety using a fake dispatcher."""

import json
from dataclasses import replace

import pytest

from benchmarks.evaluation.memory_qmsum_protocol import QMSumProtocolError
from benchmarks.evaluation.memory_qmsum_summary_runner import run_summary_candidates
from tests.unit.test_memory_qmsum_summary import _prepare, _prepared_case


@pytest.fixture(name="prepared_case")
def runner_prepared_case():
    return _prepared_case()


@pytest.mark.asyncio
async def test_summary_attempts_stop_on_uncertain_outcome_and_preserve_unattempted(prepared_case):
    source, plan, question, policy = prepared_case
    first = _prepare(source, plan, question, policy)
    requests = tuple(
        replace(first, question_id=item.question_id) for item in source.questions[:4]
    )
    source_id = first.context.sources[0].source_id
    candidate = json.dumps(
        {
            "status": "OK",
            "goal": {"text": "continue", "source_ids": [source_id]},
            "decisions": [],
            "supported_facts": [],
            "conflicts": [],
            "open_questions": [],
            "completed_actions_refs": [],
            "next_steps": [],
            "omitted_source_ids": [],
        }
    )
    calls = []

    async def dispatch(preparation):
        calls.append(preparation.question_id)
        if len(calls) == 3:
            raise OSError("provider outcome unknown")
        return {
            "attempt_id": f"attempt-{len(calls)}",
            "content": candidate,
            "finish_reason": "stop" if len(calls) == 1 else "length",
        }

    result = await run_summary_candidates(requests, dispatch=dispatch)
    assert calls == [item.question_id for item in requests[:3]]
    assert [item.status for item in result.observations] == [
        "VALID",
        "INVALID",
        "UNKNOWN_OUTCOME",
    ]
    assert result.observations[1].issues == ("PROVIDER_RESPONSE_INCOMPLETE",)
    assert result.unattempted_question_ids == (requests[3].question_id,)
    assert all("content" not in row.measurements for row in result.observations)


@pytest.mark.asyncio
async def test_duplicate_question_is_rejected_before_any_dispatch(prepared_case):
    source, plan, question, policy = prepared_case
    preparation = _prepare(source, plan, question, policy)
    calls = []

    async def dispatch(item):
        calls.append(item)
        return {}

    with pytest.raises(QMSumProtocolError, match="one summary preparation"):
        await run_summary_candidates((preparation, preparation), dispatch=dispatch)
    assert not calls
