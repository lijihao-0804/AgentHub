"""Production knowledge retrieval component composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

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

_components_lock = Lock()
_components_cache: dict[tuple[object, ...], RetrievalComponents] = {}


def production_retrieval_components(settings: Settings) -> RetrievalComponents:
    """Build and cache real process-local adapters; tests inject their own factory."""

    key = (
        settings.knowledge_embedding_model,
        settings.knowledge_embedding_device,
        settings.knowledge_embedding_batch_size,
        settings.knowledge_dense_vector_size,
        settings.knowledge_reranker_model,
        settings.knowledge_reranker_device,
        settings.knowledge_reranker_batch_size,
        settings.qdrant_url,
        settings.knowledge_qdrant_collection,
        settings.knowledge_qdrant_timeout_seconds,
    )
    with _components_lock:
        cached = _components_cache.get(key)
        if cached is not None:
            return cached
        components = RetrievalComponents(
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
        _components_cache[key] = components
        return components


def close_production_retrieval_components() -> None:
    """Close and clear cached production adapters during process shutdown."""

    with _components_lock:
        components = tuple(_components_cache.values())
        _components_cache.clear()
    for item in components:
        close = getattr(item.index, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                # Shutdown is best effort; never mask the owning process exit.
                pass


__all__ = [
    "RetrievalComponents",
    "RetrievalComponentsFactory",
    "production_retrieval_components",
    "close_production_retrieval_components",
]
