"""Experimental cross-encoder scoring of source spans inside already-retrieved chunks."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

from packages.knowledge.contracts import KnowledgeProviderError, RerankCandidate, Reranker
from packages.knowledge.structural_chunking import _paragraph_spans, _sentence_spans

_ARTICLE_QUESTION = re.compile(
    r'^\s*in the (?:paper|article|document)\s+(?:“[^”]+”|"[^"]+")\s*,\s*(\S.*)$',
    re.IGNORECASE | re.DOTALL,
)


def evidence_question(query: str) -> str:
    """Keep the information need from a query that explicitly names a source."""
    match = _ARTICLE_QUESTION.fullmatch(query)
    return match.group(1) if match else query


def sentence_offsets(text: str, width: int = 2) -> tuple[tuple[int, int], ...]:
    """Sliding windows of adjacent source sentences without crossing paragraph boundaries."""
    if width < 1:
        raise ValueError("a sentence window must include at least one sentence")
    windows = []
    for paragraph in _paragraph_spans(text):
        spans = _sentence_spans(text, paragraph)
        size = min(width, len(spans))
        for first in range(len(spans) - size + 1):
            windows.append((spans[first].start, spans[first + size - 1].end))
    return tuple(windows)


@dataclass(frozen=True)
class SentenceScore:
    start: int
    end: int
    score: float


@dataclass(frozen=True)
class ChunkScore:
    chunk_id: str
    whole_score: float
    score: float
    windows: tuple[SentenceScore, ...]


class SentenceEvidenceReranker:
    """Score a candidate pool and return one logit per original source chunk."""

    def __init__(self, base_reranker: Reranker) -> None:
        self.base = base_reranker

    def inspect(self, query: str, candidates: Sequence[RerankCandidate]) -> tuple[ChunkScore, ...]:
        whole = self.base.rerank(query, candidates)
        spans = [sentence_offsets(candidate.text) for candidate in candidates]
        unique = dict.fromkeys(
            candidate.text[start:end]
            for candidate, offsets in zip(candidates, spans, strict=True)
            for start, end in offsets
        )
        probes = [RerankCandidate(str(i), text) for i, text in enumerate(unique)]
        logits = dict(zip(unique, self.base.rerank(evidence_question(query), probes), strict=True))
        if not all(isfinite(score) for score in (*whole, *logits.values())):
            raise KnowledgeProviderError("INVALID_RERANK_RESULT", "Nonfinite evidence score.")
        rows = []
        for candidate, full, offsets in zip(candidates, whole, spans, strict=True):
            windows = tuple(
                SentenceScore(start, end, logits[candidate.text[start:end]])
                for start, end in offsets
            )
            best = max((part.score for part in windows), default=full)
            rows.append(ChunkScore(candidate.chunk_id, full, (full + best) / 2, windows))
        return tuple(rows)

    def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> tuple[float, ...]:
        return tuple(item.score for item in self.inspect(query, candidates))
