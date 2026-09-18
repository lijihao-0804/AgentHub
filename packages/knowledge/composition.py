"""Production knowledge retrieval component composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from packages.core.config.settings import Settings
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.contracts import DenseEmbedder, Reranker, SparseEncoder, VectorIndex


@dataclass(frozen=True)
class RetrievalComponents:
    dense: DenseEmbedder
    sparse: SparseEncoder
    reranker: Reranker
    index: VectorIndex


RetrievalComponentsFactory = Callable[[Settings], RetrievalComponents]


def production_retrieval_components(settings: Settings) -> RetrievalComponents:
    """Build the real production adapters; tests must inject their own factory."""

    return RetrievalComponents(
        dense=BgeM3DenseEmbedder(
            model_name=settings.knowledge_embedding_model,
            device=settings.knowledge_embedding_device,
            batch_size=settings.knowledge_embedding_batch_size,
            expected_dimension=settings.knowledge_dense_vector_size,
        ),
        sparse=BgeM3SparseEncoder(model_name=settings.knowledge_embedding_model),
        reranker=BgeReranker(
            model_name=settings.knowledge_reranker_model,
            device=settings.knowledge_reranker_device,
            batch_size=settings.knowledge_reranker_batch_size,
        ),
        index=QdrantVectorIndex(
            url=settings.qdrant_url,
            collection_name=settings.knowledge_qdrant_collection,
            dense_vector_size=settings.knowledge_dense_vector_size,
            timeout_seconds=settings.knowledge_qdrant_timeout_seconds,
        ),
    )


__all__ = [
    "RetrievalComponents",
    "RetrievalComponentsFactory",
    "production_retrieval_components",
]
