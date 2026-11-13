"""Candidate policies operating on ordered, snapshot-scoped chunk retrieval lanes."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from heapq import nsmallest
from typing import TypedDict

from benchmarks.public_rag.project_score_fusion import fuse_dbsf


class Hit(TypedDict):
    chunk_id: str
    document_id: str
    score: float


def _check_lanes(lanes: Sequence[Sequence[Hit]]) -> None:
    sources: dict[str, str] = {}
    for lane in lanes:
        seen = set()
        for hit in lane:
            chunk, doc = hit["chunk_id"], hit["document_id"]
            if not chunk or not doc or chunk in seen or not math.isfinite(hit["score"]):
                raise ValueError("invalid or duplicate candidate in retriever lane")
            if sources.setdefault(chunk, doc) != doc:
                raise ValueError("a candidate has conflicting document IDs")
            seen.add(chunk)


def _rrf(lanes: Sequence[Sequence[Hit]], k: int) -> list[Hit]:
    """Extend production's score, best-source-rank, chunk-ID ordering to N lanes."""
    scores: dict[str, float] = defaultdict(float)
    best: dict[str, int] = {}
    docs: dict[str, str] = {}
    for lane in lanes:
        for rank, hit in enumerate(lane, start=1):
            chunk = hit["chunk_id"]
            scores[chunk] += 1 / (k + rank)
            best[chunk] = min(best.get(chunk, rank), rank)
            docs[chunk] = hit["document_id"]
    ordered = sorted(scores, key=lambda chunk: (-scores[chunk], best[chunk], chunk))
    return [Hit(chunk_id=c, document_id=docs[c], score=scores[c]) for c in ordered]


def _group_first(ranked: Sequence[Hit], limit: int) -> list[Hit]:
    first, deferred = [], []
    seen = set()
    for hit in ranked:
        (deferred if hit["document_id"] in seen else first).append(hit)
        seen.add(hit["document_id"])
    return (first + deferred)[:limit]


def _refill(baseline: list[Hit], alternatives: Sequence[Hit], *, lexical: bool) -> list[Hit]:
    seen_documents: set[str] = set()
    replaceable = []
    for index, hit in enumerate(baseline):
        doc = hit["document_id"]
        if index >= 8 and doc in seen_documents:
            replaceable.append(index)
        seen_documents.add(doc)
    capacity = min(4, len(replaceable))
    if not capacity:
        return baseline
    additions = []
    for hit in alternatives:
        doc = hit["document_id"]
        if doc in seen_documents:
            continue
        seen_documents.add(doc)
        score = baseline[-1]["score"] / (2 + len(additions)) if lexical else hit["score"]
        additions.append(Hit(chunk_id=hit["chunk_id"], document_id=doc, score=score))
        if len(additions) == capacity:
            break
    removed = set(replaceable[-len(additions) :]) if additions else set()
    return [hit for i, hit in enumerate(baseline) if i not in removed] + additions


def select_candidates(
    method: str,
    dense: Sequence[Hit],
    sparse: Sequence[Hit],
    lexical: Sequence[Hit] = (),
    *,
    limit: int = 20,
) -> list[Hit]:
    """Return at most the fixed number of unique chunks. Preserve route order and scores."""
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be positive")
    dense, sparse, lexical = dense[:30], sparse[:30], lexical[:20]
    _check_lanes((dense, sparse, lexical))
    if method == "lexical_three":
        return _rrf((dense[:20], sparse[:20], lexical), 60)[:limit]
    if method == "dbsf":
        return fuse_dbsf((dense, sparse), limit=limit)
    ranked = _rrf((dense, sparse), 1 if method == "rrf_head" else 60)
    baseline = ranked[:limit]
    if method in ("baseline", "rrf_head"):
        return baseline
    if method == "group_first":
        return _group_first(ranked, limit)
    if method == "safe_refill4":
        return _refill(baseline, ranked[limit:], lexical=False)
    if method == "lexical_refill4":
        return _refill(baseline, lexical, lexical=True)
    raise ValueError(f"unknown candidate policy: {method}")


_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def lexical_terms(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", text).casefold())


class WordBM25:
    """In-memory inverted index over the complete authorized chunk snapshot."""

    def __init__(self, chunks: Mapping[str, tuple[str, str]]):
        if not chunks:
            raise ValueError("BM25 requires at least one chunk")
        self._ids = list(chunks)
        self._sources = [chunks[c][0] for c in self._ids]
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        lengths = []
        for index, chunk in enumerate(self._ids):
            tokens = lexical_terms(chunks[chunk][1])
            lengths.append(len(tokens))
            for word, count in Counter(tokens).items():
                postings[word].append((index, count))
        count = len(lengths)
        mean_length = sum(lengths) / count or 1.0
        self._norm = [1.2 * (0.25 + 0.75 * length / mean_length) for length in lengths]
        self._postings = dict(postings)
        self._idf = {
            word: math.log1p((count - len(matches) + 0.5) / (len(matches) + 0.5))
            for word, matches in self._postings.items()
        }

    def query(self, text: str, *, limit: int = 20) -> list[Hit]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be positive")
        scores: dict[int, float] = defaultdict(float)
        for word in dict.fromkeys(lexical_terms(text)):
            for index, freq in self._postings.get(word, ()):
                scores[index] += self._idf[word] * freq * 2.2 / (freq + self._norm[index])
        best = nsmallest(limit, scores, key=lambda i: (-scores[i], self._ids[i]))
        return [
            Hit(chunk_id=self._ids[i], document_id=self._sources[i], score=scores[i]) for i in best
        ]
