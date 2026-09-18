"""Deterministic model fakes used by tests and CI only."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from packages.knowledge.contracts import RerankCandidate, SparseEncoding


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+|[^\w\s]", text.casefold(), flags=re.UNICODE))


def _sparse(text: str) -> SparseEncoding:
    counts: dict[int, int] = {}
    for token in _tokens(text):
        index = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16) + 1
        counts[index] = counts.get(index, 0) + 1
    indices = tuple(sorted(counts))
    values = tuple(1.0 + math.log(counts[index]) for index in indices)
    return SparseEncoding(indices=indices, values=values)


class DeterministicFakeDenseEmbedder:
    def __init__(self, dimension: int = 1024) -> None:
        if dimension < 1:
            raise ValueError("dimension must be positive")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self.embed_query(text) for text in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        values: list[float] = []
        counter = 0
        while len(values) < self._dimension:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            values.extend((byte / 127.5) - 1.0 for byte in digest)
            counter += 1
        vector = values[: self._dimension]
        norm = math.sqrt(sum(value * value for value in vector))
        return tuple(value / norm for value in vector) if norm else tuple(vector)


class DeterministicFakeSparseEncoder:
    def encode_documents(self, texts: Sequence[str]) -> tuple[SparseEncoding, ...]:
        return tuple(self.encode_query(text) for text in texts)

    def encode_query(self, text: str) -> SparseEncoding:
        return _sparse(text)


class DeterministicFakeReranker:
    def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> tuple[float, ...]:
        query_tokens = set(_tokens(query))
        return tuple(
            float(len(query_tokens.intersection(_tokens(candidate.text))))
            + (int(hashlib.sha256(candidate.chunk_id.encode("utf-8")).hexdigest()[:8], 16) / 2**40)
            for candidate in candidates
        )


__all__ = [
    "DeterministicFakeDenseEmbedder",
    "DeterministicFakeReranker",
    "DeterministicFakeSparseEncoder",
]
