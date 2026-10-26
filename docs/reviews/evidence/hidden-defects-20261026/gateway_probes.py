"""Gateway and retrieval boundary probes using local fakes only."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from packages.knowledge.adapters.onyx_selection import OpenAICompatibleSectionSelector
from packages.knowledge.onyx_retriever import OnyxSelectionKnowledgeRetriever
from packages.model_gateway.contracts import ModelResponse
from packages.model_gateway.gateway import ModelGatewayService
from tests.unit.test_model_gateway_service import (
    FakeAdapter,
    FakeRepository,
    request,
    response,
    setup_chain,
)


@pytest.mark.asyncio
async def test_empty_primary_response_skips_healthy_fallback():
    context, profiles, credential = setup_chain()
    adapter = FakeAdapter()
    adapter.complete_outcomes = {
        "primary": [
            ModelResponse(content="", provider="fake", model="primary", finish_reason="stop")
        ],
        "fallback": [response("fallback")],
    }
    gateway = ModelGatewayService(FakeRepository(profiles, [credential]), adapter)
    result = await gateway.generate(context, profiles[0].id, request())
    assert result.content == ""
    assert adapter.complete_calls == ["primary"]


def test_subsecond_qdrant_timeout_is_passed_as_zero(monkeypatch):
    from packages.core.config.settings import Settings
    from packages.knowledge.adapters import qdrant

    settings = Settings(testing=True, knowledge_qdrant_timeout_seconds=0.5)
    calls = []

    def client(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(qdrant, "QdrantClient", client)
    qdrant.QdrantVectorIndex(
        url="http://unused.invalid",
        collection_name="audit",
        dense_vector_size=8,
        timeout_seconds=settings.knowledge_qdrant_timeout_seconds,
    )
    assert calls[0]["timeout"] == 0


@pytest.mark.asyncio
async def test_selector_discards_reported_usage():
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "model": "audit",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                "choices": [{"message": {"content": '{"section_ids":[0]}'}}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        selector = OpenAICompatibleSectionSelector(
            base_url="http://unused.invalid/v1",
            api_key="not-a-real-secret",
            model="audit",
            client=client,
        )
        result = await selector.select_sections(
            SimpleNamespace(),
            "question",
            [
                {
                    "section_id": 0,
                    "source_id": "source",
                    "title": "title",
                    "content": "evidence",
                    "chunk_ids": ["c1"],
                }
            ],
        )
    assert result == (0,)
    assert not hasattr(result, "usage")


@pytest.mark.asyncio
async def test_outer_timeout_prevents_selector_ranking_fallback():
    from tests.unit.test_onyx_selection_optin import _query
    from tests.unit.test_runtime_rag_evidence_projection import _record

    class Base:
        async def retrieve_with_trace(self, *_args):
            return SimpleNamespace(evidence=(_record(1),))

    class HangingSelector:
        async def select_sections(self, *_args):
            await asyncio.Event().wait()

    from unittest.mock import patch

    import packages.knowledge.onyx_retriever as module

    retriever = OnyxSelectionKnowledgeRetriever(Base(), HangingSelector())
    with patch.object(module, "sections_from_evidence", return_value=[{}]):
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await retriever.retrieve_with_trace(SimpleNamespace(), _query())
