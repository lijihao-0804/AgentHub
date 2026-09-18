from __future__ import annotations

import math

import pytest
from qdrant_client import QdrantClient

from apps.worker.tasks.knowledge import _indexing_components
from packages.core.config.settings import Settings
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.fakes import (
    DeterministicFakeDenseEmbedder,
    DeterministicFakeReranker,
    DeterministicFakeSparseEncoder,
)
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.contracts import (
    KnowledgeProviderError,
    RerankCandidate,
    VectorRecord,
    VectorScope,
    provider_error_is_retryable,
)
from packages.knowledge.point_ids import deterministic_point_id


def test_point_id_and_fake_models_are_deterministic() -> None:
    dense = DeterministicFakeDenseEmbedder(dimension=8)
    sparse = DeterministicFakeSparseEncoder()
    reranker = DeterministicFakeReranker()

    assert deterministic_point_id("chunk-1") == deterministic_point_id("chunk-1")
    assert deterministic_point_id("chunk-1") != deterministic_point_id("chunk-2")
    assert dense.embed_query("中文 and English") == dense.embed_query("中文 and English")
    sparse_result = sparse.encode_query("中文 中文 English")
    assert sparse_result.indices == tuple(sorted(sparse_result.indices))
    assert all(math.isfinite(value) and value >= 0 for value in sparse_result.values)
    candidates = (RerankCandidate("chunk-1", "中文 English"),)
    assert reranker.rerank("中文", candidates) == reranker.rerank("中文", candidates)


def test_real_dense_adapter_is_lazy() -> None:
    adapter = BgeM3DenseEmbedder(expected_dimension=8)
    assert adapter.dimension == 8
    assert adapter._model is None


def test_testing_setting_does_not_select_fake_production_components() -> None:
    settings = Settings(testing=True, knowledge_dense_vector_size=8)
    components = _indexing_components(settings)
    assert isinstance(components.dense, BgeM3DenseEmbedder)
    assert components.dense._model is None


def test_provider_error_retry_classification_is_explicit() -> None:
    assert provider_error_is_retryable("QDRANT_UNAVAILABLE")
    assert provider_error_is_retryable("EMBEDDER_LOAD_FAILED")
    assert provider_error_is_retryable("SPARSE_ENCODING_FAILED")
    assert not provider_error_is_retryable("CUDA_UNAVAILABLE")
    assert not provider_error_is_retryable("EMBEDDER_DIMENSION_MISMATCH")
    assert not provider_error_is_retryable("INVALID_PROVIDER_RESULT")
    assert not provider_error_is_retryable("UNKNOWN_PROVIDER_ERROR")


def test_qdrant_collection_is_idempotent_and_duplicate_upsert_is_safe() -> None:
    client = QdrantClient(":memory:")
    index = QdrantVectorIndex(
        url="http://unused",
        collection_name="knowledge",
        dense_vector_size=8,
        client=client,
    )
    index.ensure_collection()
    index.ensure_collection()
    dense = DeterministicFakeDenseEmbedder(dimension=8).embed_query("hello")
    sparse = DeterministicFakeSparseEncoder().encode_query("hello")
    record = VectorRecord(
        point_id=deterministic_point_id("chunk-1"),
        chunk_id="chunk-1",
        dense=dense,
        sparse=sparse,
        payload={
            "workspace_id": "workspace-a",
            "knowledge_base_id": "kb-a",
            "document_id": "document-a",
            "document_revision_id": "revision-a",
            "chunk_id": "chunk-1",
            "locator": {"type": "text_range", "char_start": 0, "char_end": 5},
        },
    )
    index.upsert((record,))
    index.upsert((record,))

    hits = index.dense_search(
        dense,
        scope=VectorScope("workspace-a", "kb-a", ("revision-a",)),
        limit=30,
    )
    assert [hit.payload["chunk_id"] for hit in hits] == ["chunk-1"]
    assert index.dense_search(
        dense,
        scope=VectorScope("workspace-b", "kb-a", ("revision-a",)),
        limit=30,
    ) == ()
    assert index.dense_search(
        dense,
        scope=VectorScope("workspace-a", "kb-other", ("revision-a",)),
        limit=30,
    ) == ()


def test_qdrant_schema_mismatch_does_not_recreate_collection() -> None:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name="knowledge",
        vectors_config={"dense": {"size": 4, "distance": "Cosine"}},
        sparse_vectors_config={"sparse": {}},
    )
    index = QdrantVectorIndex(
        url="http://unused",
        collection_name="knowledge",
        dense_vector_size=8,
        client=client,
    )
    with pytest.raises(KnowledgeProviderError) as error:
        index.ensure_collection()
    assert error.value.code == "QDRANT_SCHEMA_MISMATCH"


def test_qdrant_sparse_modifier_idf_passes_and_non_idf_fails() -> None:
    idf_client = QdrantClient(":memory:")
    idf_client.create_collection(
        collection_name="idf",
        vectors_config={"dense": {"size": 8, "distance": "Cosine"}},
        sparse_vectors_config={"sparse": {"modifier": "idf"}},
    )
    QdrantVectorIndex(
        url="http://unused",
        collection_name="idf",
        dense_vector_size=8,
        client=idf_client,
    ).ensure_collection()

    non_idf_client = QdrantClient(":memory:")
    non_idf_client.create_collection(
        collection_name="non-idf",
        vectors_config={"dense": {"size": 8, "distance": "Cosine"}},
        sparse_vectors_config={"sparse": {"modifier": "none"}},
    )
    index = QdrantVectorIndex(
        url="http://unused",
        collection_name="non-idf",
        dense_vector_size=8,
        client=non_idf_client,
    )
    with pytest.raises(KnowledgeProviderError) as error:
        index.ensure_collection()
    assert error.value.code == "QDRANT_SCHEMA_MISMATCH"
