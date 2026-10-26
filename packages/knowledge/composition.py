"""Production knowledge retrieval component composition."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

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

logger = logging.getLogger(__name__)

_components_lock = Lock()
_components_cache: dict[tuple[object, ...], RetrievalComponents] = {}


def _model_name(component: object) -> str | None:
    value = getattr(component, "model_name", None)
    return value if isinstance(value, str) and value else None


def _retrieval_components(retriever: object) -> tuple[object, ...]:
    components = getattr(retriever, "components", None)
    if components is not None:
        return (
            getattr(components, "dense", None),
            getattr(components, "sparse", None),
            getattr(components, "reranker", None),
        )
    return (
        getattr(retriever, "dense_embedder", None),
        getattr(retriever, "sparse_encoder", None),
        getattr(retriever, "reranker", None),
    )


def retrieval_model_identity_mismatches(
    retrieval_config: Mapping[str, Any] | None,
    retriever: object,
) -> tuple[tuple[str, str, str], ...]:
    """Return frozen/current model identity drift that must fail closed.

    Legacy versions published before model names became part of retrieval
    configuration remain executable. Test doubles and custom adapters that do
    not expose an identity also remain compatible. Production BGE adapters do
    expose it, so a deployment changed after publication cannot silently run
    a historical version against another model.
    """

    if not isinstance(retrieval_config, Mapping):
        return ()
    dense, sparse, reranker = _retrieval_components(retriever)
    checks = (
        ("embedding_model", dense),
        ("embedding_model", sparse),
        ("reranker_model", reranker),
    )
    mismatches: list[tuple[str, str, str]] = []
    for field, component in checks:
        frozen = retrieval_config.get(field)
        actual = _model_name(component)
        if isinstance(frozen, str) and frozen and actual is not None and frozen != actual:
            mismatch = (field, frozen, actual)
            if mismatch not in mismatches:
                mismatches.append(mismatch)
    return tuple(mismatches)


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


def onyx_section_selector_from_settings(settings: Settings):
    """Build the opt-in section selector, or None when unconfigured.

    Partial configuration is treated as unconfigured on purpose: a selector
    that exists but cannot authenticate would turn every opt-in retrieval
    into a doomed call followed by a fallback. The tool-level wrapper marks
    the degradation in the trace either way.
    """
    from packages.knowledge.adapters.onyx_selection import OpenAICompatibleSectionSelector

    # getattr rather than attribute access: worker/API unit tests build fake
    # settings objects that predate these keys and must keep working.
    base_url = (getattr(settings, "knowledge_onyx_selector_base_url", None) or "").strip()
    api_key = (getattr(settings, "knowledge_onyx_selector_api_key", None) or "").strip()
    model = getattr(settings, "knowledge_onyx_selector_model", None) or "deepseek-v4.1-flash-1017"
    timeout = getattr(settings, "knowledge_onyx_selector_timeout_seconds", None) or 30.0
    if not base_url or not api_key:
        return None
    return OpenAICompatibleSectionSelector(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout,
    )


def warm_retrieval_components(settings: Settings) -> None:
    """Load the retrieval models before anything asks a question of them.

    If startup warming has not completed and the model cache is cold, the first
    ``search_knowledge`` call loads BGE-M3 and its cross-encoder inside the
    tool's own timeout budget -- about 35 seconds against a 30 second ceiling
    in the API-container measurement from 2026-09-22. In that configuration,
    the first retrieval can come back as TOOL_TIMEOUT and the agent may answer
    "I could not find it". This does not apply once the process has warmed the
    models or already has them resident in its process cache.

    Best effort on purpose: a process that cannot load the models should still
    serve every route that does not need them, and fail loudly at the point of
    use, where the error carries a request id.
    """

    components = production_retrieval_components(settings)
    for part in (components.dense, components.sparse, components.reranker):
        warm = getattr(part, "warm", None)
        if warm is None:
            continue
        try:
            warm()
        except Exception:
            logger.warning(
                "knowledge_model_warm_failed",
                extra={"component": type(part).__name__},
                exc_info=True,
            )


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
    "onyx_section_selector_from_settings",
    "production_retrieval_components",
    "retrieval_model_identity_mismatches",
    "warm_retrieval_components",
    "close_production_retrieval_components",
]
