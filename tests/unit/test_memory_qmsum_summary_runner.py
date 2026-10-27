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


def _candidate(preparation):
    source_id = preparation.context.sources[0].source_id
    return json.dumps(
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


@pytest.mark.asyncio
async def test_summary_attempts_stop_on_uncertain_outcome_and_preserve_unattempted(prepared_case):
    source, plan, question, policy = prepared_case
    first = _prepare(source, plan, question, policy)
    requests = tuple(
        replace(first, question_id=item.question_id) for item in source.questions[:4]
    )
    candidate = _candidate(first)
    calls = []

    async def dispatch(preparation):
        calls.append(preparation.question_id)
        if len(calls) == 3:
            raise OSError("provider outcome unknown")
        return {
            "attempt_id": f"attempt-{len(calls)}",
            "content": candidate,
            "finish_reason": "stop" if len(calls) == 1 else "length",
            "input_tokens": preparation.admitted.usage.available_input // 2,
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
async def test_actual_input_over_budget_invalidates_even_a_well_formed_response(prepared_case):
    source, plan, question, policy = prepared_case
    preparation = _prepare(source, plan, question, policy)
    candidate = _candidate(preparation)

    async def dispatch(item):
        return {
            "attempt_id": "over-budget",
            "content": candidate,
            "finish_reason": "stop",
            "input_tokens": item.admitted.usage.available_input + 1,
        }

    result = await run_summary_candidates((preparation,), dispatch=dispatch)
    assert result.observations[0].status == "INVALID"
    assert result.observations[0].issues == ("PROVIDER_INPUT_EXCEEDED_BUDGET",)
    assert result.observations[0].candidate is None


@pytest.mark.asyncio
async def test_changed_provider_count_invalidates_candidate_even_below_budget(prepared_case):
    source, plan, question, policy = prepared_case
    preparation = _prepare(source, plan, question, policy)

    async def dispatch(item):
        return {
            "attempt_id": "counter-mismatch",
            "content": _candidate(item),
            "finish_reason": "stop",
            "input_tokens": item.admitted.usage.available_input // 2,
            "input_counter": {"tokens": item.admitted.usage.available_input // 2 - 1},
        }

    result = await run_summary_candidates((preparation,), dispatch=dispatch)
    assert result.observations[0].status == "INVALID"
    assert result.observations[0].issues == ("PROVIDER_COUNTER_MISMATCH",)


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
