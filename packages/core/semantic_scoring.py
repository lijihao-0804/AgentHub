"""Bounded, fail-closed semantic similarity scorer for small isolated candidate sets.

Designed for tenant- and user-isolated sets already filtered by SQL (e.g. an
agent's active long-term memories or one thread's prior turns). Keeps an
in-memory bounded LRU vector cache keyed by text SHA-256 and never raises into
online retrieval paths — any embedder error returns ``None`` so callers degrade
cleanly to deterministic lexical ranking.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections import OrderedDict
from collections.abc import Sequence
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_MAX_CACHE_ENTRIES = 2_048


class TextEmbedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...

    def embed_query(self, text: str) -> tuple[float, ...]: ...


def _text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


class SemanticTextScorer:
    """Fail-closed semantic cosine scorer with bounded content-hash vector cache."""

    def __init__(
        self,
        embedder: TextEmbedder,
        *,
        max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
    ) -> None:
        self._embedder = embedder
        self._max_cache_entries = max(16, int(max_cache_entries))
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()

    def _get_cached(self, key: str) -> tuple[float, ...] | None:
        vec = self._cache.get(key)
        if vec is not None:
            self._cache.move_to_end(key)
        return vec

    def _put_cached(self, key: str, vec: tuple[float, ...]) -> None:
        self._cache[key] = vec
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_cache_entries:
            self._cache.popitem(last=False)

    def score_documents(
        self,
        query: str,
        documents: Sequence[str],
    ) -> tuple[float, ...] | None:
        """Return cosine similarity for each document, or None on any embedder failure."""
        if not query.strip() or not documents:
            return tuple(0.0 for _ in documents)
        try:
            query_vec = tuple(self._embedder.embed_query(query))
            keys = [_text_key(doc) for doc in documents]
            missing_indices: list[int] = []
            missing_texts: list[str] = []
            for idx, (doc, key) in enumerate(zip(documents, keys, strict=True)):
                if self._get_cached(key) is None:
                    missing_indices.append(idx)
                    missing_texts.append(doc)
            if missing_texts:
                embedded = self._embedder.embed_documents(missing_texts)
                if len(embedded) != len(missing_texts):
                    return None
                for idx, vec in zip(missing_indices, embedded, strict=True):
                    self._put_cached(keys[idx], tuple(vec))
            scores: list[float] = []
            for key in keys:
                doc_vec = self._get_cached(key)
                if doc_vec is None:
                    return None
                scores.append(round(cosine_similarity(query_vec, doc_vec), 4))
            return tuple(scores)
        except Exception:
            logger.warning("semantic_text_scorer_failed_falling_back", exc_info=True)
            return None


__all__ = [
    "DEFAULT_MAX_CACHE_ENTRIES",
    "SemanticTextScorer",
    "TextEmbedder",
    "cosine_similarity",
]
