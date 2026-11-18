from types import SimpleNamespace
from uuid import UUID

import pytest

from benchmarks.retrieval.experiments.output_limit.endpoint import BASE_URL, MODEL, ReplayEndpoint
from packages.knowledge import citation_qa as qa
from packages.knowledge.contracts import RetrievedEvidence


@pytest.mark.asyncio
async def test_raw_messages_stay_fixed_and_payload_drift_is_stopped(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    doc, revision = str(UUID(int=11)), str(UUID(int=12))
    source = f"document:{doc}/revision:{revision}"
    text = "The dispatch counter closes at noon."
    locator = {"type": "text_range", "char_start": 0, "char_end": len(text)}
    evidence = RetrievedEvidence(doc, revision, "chunk-a", source, locator, text, 1.0, 1.0, {})
    q = "When does the dispatch counter close?"
    selected = qa._select_evidence((evidence,), max_chars=16_000)
    case = dict(
        id="example",
        question=q,
        kb=str(UUID(int=13)),
        snapshot=str(UUID(int=14)),
        selected=[
            dict(
                id=1,
                document=doc,
                revision=revision,
                chunk_id="chunk-a",
                source=source,
                locator=locator,
                text=text,
                retrieval_score=1.0,
                rerank_score=1.0,
            )
        ],
        messages=[
            dict(role="system", content=qa.citation_system_prompt("PROMPT_JSON")),
            dict(role="user", content=qa._evidence_prompt(q, selected)),
        ],
    )
    route = SimpleNamespace(
        base_url=BASE_URL, model=MODEL, provider="openai-compatible", api_key="offline"
    )
    async with ReplayEndpoint(route) as endpoint:
        low, low_events = await endpoint.query(case, 256)
        high, high_events = await endpoint.query(case, 2048)
        assert low.status_code == high.status_code == 200
        assert low_events[0]["messages_sha256"] == high_events[0]["messages_sha256"]
        assert low_events[0]["request_sha256"] != high_events[0]["request_sha256"]
        with pytest.raises(ValueError, match="physical request differs"):
            await endpoint.query({**case, "registered_wire": low_events[0]["request_sha256"]}, 2048)
    with pytest.raises(ValueError, match="Unexpected provider route"):
        ReplayEndpoint(route, meter=object())
