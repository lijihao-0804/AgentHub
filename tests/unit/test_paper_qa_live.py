import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.public_rag.paper_qa_live import (
    MODEL,
    RecordedClient,
    execution_status,
    last_visible_records,
    render_paper,
)


def test_upload_contains_article_only_and_paragraph_ranges_match_original_text():
    paper = {
        "paper_id": "p",
        "title": "Original title",
        "abstract": "Original abstract.",
        "sections": [{"heading": "Method", "paragraphs": ["The method uses samples.", "  \n "]}],
        "qas": [{"question": "PRIVATE GOLD QUESTION", "answer": "PRIVATE LABEL"}],
    }
    text, spans = render_paper(paper)
    assert "PRIVATE" not in text
    assert [text[p["start"] : p["end"]] for p in spans] == [
        "Original abstract.",
        "The method uses samples.",
    ]


def test_evidence_scores_use_actual_final_model_input_not_raw_retriever_hits():
    calls = [
        {"status": "VALID", "tool_calls": [{"name": "search_knowledge"}], "messages": []},
        {
            "status": "VALID",
            "tool_calls": [],
            "messages": [
                {
                    "role": "tool",
                    "content": json.dumps(
                        {"data": {"results": [{"chunk_id": "visible", "snippet": "short"}]}}
                    ),
                }
            ],
        },
    ]
    assert last_visible_records(calls) == [{"chunk_id": "visible", "snippet": "short"}]
    calls[-1]["status"] = "FAILED"
    assert last_visible_records(calls) == []


@pytest.mark.asyncio
async def test_physical_limit_is_shared_across_separate_run_clients(monkeypatch):
    import litellm

    call = AsyncMock(return_value=SimpleNamespace(model=MODEL))
    monkeypatch.setattr(litellm, "acompletion", call)
    shared = {"dispatched": 0, "limit": 1}
    args = {"model": "openai/" + MODEL, "max_tokens": 1024, "num_retries": 0}
    await RecordedClient(StringIO(), {}, shared).acompletion(**args)
    with pytest.raises(ValueError, match="guard stopped"):
        await RecordedClient(StringIO(), {}, shared).acompletion(**args)
    assert call.await_count == 1
    assert shared["dispatched"] == 1


def test_execution_cannot_pass_when_a_persisted_run_skipped_required_retrieval():
    results = {"arm": {"run_status": "SUCCEEDED", "workflow_valid": False}}
    status = execution_status(results, 1, False)
    assert status["execution_complete"] is True
    assert status["all_required_workflows_complete"] is False
    assert status["run_state"] == "NEEDS_ATTENTION"


@pytest.mark.asyncio
async def test_synthetic_status_on_connection_failure_is_not_a_provider_response(monkeypatch):
    import litellm

    error = litellm.APIConnectionError(message="offline", llm_provider="openai", model=MODEL)
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(side_effect=error))
    client = RecordedClient(StringIO(), {}, {"dispatched": 0, "limit": 1})
    with pytest.raises(litellm.APIConnectionError):
        await client.acompletion(model="openai/" + MODEL, max_tokens=1024, num_retries=0)
    assert client.raw_calls[0]["status"] == "UNKNOWN_OUTCOME"
    assert client.halted is True
