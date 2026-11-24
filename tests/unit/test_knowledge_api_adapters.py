"""Unit tests for OpenAI-compatible dense embedder and reranker adapters."""

from __future__ import annotations

import json
import math

import httpx
import pytest

from packages.core.config.settings import Settings
from packages.knowledge.adapters.api_embeddings import OpenAICompatibleDenseEmbedder
from packages.knowledge.adapters.api_reranker import OpenAICompatibleReranker
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.composition import (
    close_production_retrieval_components,
    production_retrieval_components,
)
from packages.knowledge.contracts import KnowledgeProviderError, RerankCandidate


def test_api_dense_embedder_batches_sorts_by_index_and_normalizes() -> None:
    seen_batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://example.test/v1/embeddings")
        assert request.headers["Authorization"] == "Bearer secret-key"
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "qwen3.7-text-embedding"
        seen_batches.append(body["input"])
        if len(body["input"]) == 2:
            # Return out-of-order indices to verify sorting by index
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 3.0, 4.0]},
                        {"index": 0, "embedding": [3.0, 4.0, 0.0]},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.0, 0.0, 5.0]}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OpenAICompatibleDenseEmbedder(
        base_url="https://example.test/v1",
        api_key="secret-key",
        model_name="qwen3.7-text-embedding",
        expected_dimension=3,
        batch_size=2,
        client=client,
    )
    vectors = embedder.embed_documents(("first", "second", "third"))
    assert seen_batches == [["first", "second"], ["third"]]
    assert len(vectors) == 3
    assert vectors[0] == pytest.approx((0.6, 0.8, 0.0))
    assert vectors[1] == pytest.approx((0.0, 0.6, 0.8))
    assert vectors[2] == pytest.approx((0.0, 0.0, 1.0))
    assert embedder.dimension == 3
    assert len(embedder.document_cache_fingerprint()) == 64


def test_api_dense_embedder_rejects_dimension_mismatch_and_http_errors() -> None:
    def bad_dim(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    embedder = OpenAICompatibleDenseEmbedder(
        base_url="https://example.test/v1",
        api_key="k",
        model_name="m",
        expected_dimension=3,
        client=httpx.Client(transport=httpx.MockTransport(bad_dim)),
    )
    with pytest.raises(KnowledgeProviderError) as exc_info:
        embedder.embed_query("hello")
    assert exc_info.value.code == "EMBEDDER_DIMENSION_MISMATCH"

    def http_err(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    embedder_err = OpenAICompatibleDenseEmbedder(
        base_url="https://example.test/v1",
        api_key="k",
        model_name="m",
        expected_dimension=3,
        client=httpx.Client(transport=httpx.MockTransport(http_err)),
    )
    with pytest.raises(KnowledgeProviderError) as exc_info2:
        embedder_err.embed_query("hello")
    assert exc_info2.value.code == "EMBEDDER_INFERENCE_FAILED"


def test_api_reranker_supports_compatible_and_dashscope_native_endpoints() -> None:
    def compat_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://example.test/compatible-api/v1/reranks")
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "qwen3-rerank"
        assert body["documents"] == ["doc-a", "doc-b"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.12},
                ]
            },
        )

    reranker = OpenAICompatibleReranker(
        base_url="https://example.test/compatible-api/v1",
        api_key="k",
        model_name="qwen3-rerank",
        client=httpx.Client(transport=httpx.MockTransport(compat_handler)),
    )
    scores = reranker.rerank(
        "query",
        (RerankCandidate("c0", "doc-a"), RerankCandidate("c1", "doc-b")),
    )
    assert scores == pytest.approx((0.12, 0.91))

    def native_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://example.test/api/v1/services/rerank/text-rerank/text-rerank"
        )
        body = json.loads(request.content.decode("utf-8"))
        assert body["input"]["query"] == "query"
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": 0, "relevance_score": 0.75},
                        {"index": 1, "relevance_score": 0.25},
                    ]
                }
            },
        )

    native_reranker = OpenAICompatibleReranker(
        base_url="https://example.test/api/v1/services/rerank/text-rerank/text-rerank",
        api_key="k",
        model_name="gte-rerank-v2",
        client=httpx.Client(transport=httpx.MockTransport(native_handler)),
    )
    assert native_reranker.rerank(
        "query",
        (RerankCandidate("c0", "doc-a"), RerankCandidate("c1", "doc-b")),
    ) == pytest.approx((0.75, 0.25))


def test_api_reranker_rejects_malformed_or_nan_scores() -> None:
    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": "not-a-float"}]},
        )

    reranker = OpenAICompatibleReranker(
        base_url="https://example.test/v1",
        api_key="k",
        model_name="qwen3-rerank",
        client=httpx.Client(transport=httpx.MockTransport(bad_handler)),
    )
    with pytest.raises(KnowledgeProviderError) as exc_info:
        reranker.rerank("q", (RerankCandidate("c0", "doc"),))
    assert exc_info.value.code == "INVALID_RERANK_RESULT"
    assert math.isfinite(0.0)


def test_composition_preserves_local_defaults_and_supports_opt_in_api_providers() -> None:
    close_production_retrieval_components()
    try:
        local_components = production_retrieval_components(Settings())
        assert isinstance(local_components.dense, BgeM3DenseEmbedder)
        assert isinstance(local_components.reranker, BgeReranker)

        api_settings = Settings(
            knowledge_embedding_provider="openai_compatible",
            knowledge_embedding_model="qwen3.7-text-embedding",
            knowledge_embedding_api_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            knowledge_embedding_api_key="test-key",
            knowledge_reranker_provider="openai_compatible",
            knowledge_reranker_model="qwen3-rerank",
            knowledge_reranker_api_base_url="https://dashscope.aliyuncs.com/compatible-api/v1",
            knowledge_reranker_api_key="test-key",
        )
        api_components = production_retrieval_components(api_settings)
        assert isinstance(api_components.dense, OpenAICompatibleDenseEmbedder)
        assert api_components.dense.model_name == "qwen3.7-text-embedding"
        assert isinstance(api_components.reranker, OpenAICompatibleReranker)
        assert api_components.reranker.model_name == "qwen3-rerank"
        assert api_components.sparse.model_name == "BAAI/bge-m3"
    finally:
        close_production_retrieval_components()
