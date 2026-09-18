from __future__ import annotations

from apps.worker.tasks.knowledge import IndexingComponents
from packages.core.config.settings import Settings
from packages.knowledge.adapters.fakes import (
    DeterministicFakeDenseEmbedder,
    DeterministicFakeReranker,
    DeterministicFakeSparseEncoder,
)
from packages.knowledge.adapters.qdrant import QdrantVectorIndex


def fake_indexing_components(settings: Settings) -> IndexingComponents:
    """Explicit test/CI composition; production never selects this implicitly."""

    return IndexingComponents(
        dense=DeterministicFakeDenseEmbedder(dimension=settings.knowledge_dense_vector_size),
        sparse=DeterministicFakeSparseEncoder(),
        reranker=DeterministicFakeReranker(),
        index=QdrantVectorIndex(
            url=settings.qdrant_url,
            collection_name=settings.knowledge_qdrant_collection,
            dense_vector_size=settings.knowledge_dense_vector_size,
            timeout_seconds=settings.knowledge_qdrant_timeout_seconds,
        ),
    )
