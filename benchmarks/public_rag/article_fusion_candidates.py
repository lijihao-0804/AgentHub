"""Document-level reciprocal rank fusion over two authorized original chunk lanes."""

import math
from collections import Counter
from collections.abc import Mapping, Sequence

from benchmarks.public_rag.candidate_recall_methods import (
    Hit,
    _check_lanes,
    _refill,
    _rrf,
    lexical_terms,
)


def select_article_candidates(
    method: str, dense: Sequence[Hit], sparse: Sequence[Hit], *, limit: int = 20
) -> list[Hit]:
    """Select original chunks. The output score is their original chunk RRF tie-break."""
    if method not in {"article_first", "article_refill4"}:
        raise ValueError(f"unknown article fusion method: {method}")
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be positive")
    lanes = dense[:30], sparse[:30]
    _check_lanes(lanes)
    chunks = _rrf(lanes, 60)
    representatives = {}
    for hit in chunks:
        representatives.setdefault(hit["document_id"], hit)
    doc_lanes = []
    for lane in lanes:
        ids = dict.fromkeys(h["document_id"] for h in lane)
        doc_lanes.append([Hit(chunk_id=d, document_id=d, score=1.0) for d in ids])
    articles = [representatives[h["document_id"]] for h in _rrf(doc_lanes, 60)]
    if method == "article_refill4":
        return _refill(chunks[:limit], articles, lexical=False)
    first = {h["chunk_id"] for h in articles}
    return (articles + [h for h in chunks if h["chunk_id"] not in first])[:limit]


def select_local_passages(
    query: str,
    dense: Sequence[Hit],
    sparse: Sequence[Hit],
    source_chunks: Mapping[str, Sequence[dict]],
    *,
    limit: int = 20,
) -> list[Hit]:
    """Reopen selected documents only; preserve their exact article-first slot sequence."""
    slots = select_article_candidates("article_first", dense, sparse, limit=limit)
    rrf = {h["chunk_id"]: h["score"] for h in _rrf((dense[:30], sparse[:30]), 60)}
    terms = set(lexical_terms(query))
    ordered = {}
    for doc in dict.fromkeys(h["document_id"] for h in slots):
        blocks = source_chunks[doc]
        ids = [h["chunk_id"] for h in blocks]
        if len(ids) != len(set(ids)) or any(h["document_id"] != doc for h in blocks):
            raise ValueError("conflicting source blocks")
        required = {h["chunk_id"] for h in slots if h["document_id"] == doc}
        if not required.issubset(ids):
            raise ValueError("selected source revision is missing original blocks")
        word_counts = [Counter(lexical_terms(h["text"])) for h in blocks]
        mean_length = sum(c.total() for c in word_counts) / len(blocks) or 1.0
        df = Counter(w for counts in word_counts for w in sorted(terms.intersection(counts)))
        scores = []
        for counts in word_counts:
            norm = 1.2 * (0.25 + 0.75 * counts.total() / mean_length)
            scores.append(
                sum(
                    math.log1p((len(blocks) - df[w] + 0.5) / (df[w] + 0.5))
                    * counts[w]
                    * 2.2
                    / (counts[w] + norm)
                    for w in sorted(terms.intersection(counts))
                )
            )
        order = sorted(range(len(ids)), key=lambda i: (-scores[i], -rrf.get(ids[i], 0), ids[i]))
        ordered[doc] = iter([ids[i] for i in order])
    hits = []
    for slot in slots:
        doc = slot["document_id"]
        cid = next(ordered[doc])
        hits.append(Hit(chunk_id=cid, document_id=doc, score=rrf.get(cid, 0.0)))
    return hits
