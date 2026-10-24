"""P-C1 query decomposition: one gateway call, strict parsing, pure rankers.

The decomposer is single-attempt by protocol: an invalid response falls back
to the original question as the sole sub-query and is recorded, never
retried. The lexical ranker and N-way RRF live here so the gate stage stays
deterministic and unit-testable without any retrieval-model compute.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence

import httpx

DECOMPOSE_SYSTEM_PROMPT = """\
Decompose the retrieval question into independently searchable sub-questions.
Cover every intermediate entity or relation the question needs: each sub-
question must be answerable lookup on its own and together they must cover
the full chain. Use the question's own language. Do not answer anything.

Return 2 to 4 sub-questions, each a short standalone question.
Return only a JSON object with exactly this shape:
{"sub_queries": ["...", "..."]}
"""

MAX_SUB_QUERIES = 4
MAX_SUB_QUERY_CHARS = 500
RRF_K = 60
_WORD = re.compile(r"[a-z0-9]+")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def parse_decomposition(text: str) -> tuple[str, ...]:
    """Parse the exact decomposition contract; raise ValueError on deviation."""
    if not isinstance(text, str):
        raise ValueError("decomposition must be JSON text")
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("decomposition must be valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"sub_queries"}:
        raise ValueError("decomposition must contain exactly sub_queries")
    raw = payload["sub_queries"]
    if type(raw) is not list or not raw or len(raw) > MAX_SUB_QUERIES:
        raise ValueError("sub_queries must be a non-empty list of at most four items")
    sub_queries: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("each sub-query must be a string")
        stripped = item.strip()
        if not stripped or len(stripped) > MAX_SUB_QUERY_CHARS:
            raise ValueError("each sub-query must be 1..500 characters")
        if stripped not in sub_queries:
            sub_queries.append(stripped)
    if not sub_queries:
        raise ValueError("decomposition produced no usable sub-query")
    return tuple(sub_queries)


def decompose_messages(question: str) -> list[dict[str, object]]:
    return [
        {"role": "system", "content": DECOMPOSE_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]


def _observed_usage(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    output = {
        key: value[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type(value.get(key)) is int and value[key] >= 0
    }
    return output or None


async def decompose_once(
    client: httpx.AsyncClient, *, base_url: str, api_key: str, model: str, question: str
) -> dict[str, object]:
    """One decomposition call, classified exactly like the answer client."""
    result: dict[str, object] = {
        "status": "FALLBACK",
        "sub_queries": [question],
        "attempted": False,
        "request_ms": 0.0,
        "usage": None,
        "http_status": None,
    }
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 600,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": decompose_messages(question),
    }
    started = time.perf_counter()
    try:
        response = await client.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key},
            json=payload,
        )
    except httpx.RequestError:
        result["status"] = "UNKNOWN_OUTCOME"
        result["request_ms"] = (time.perf_counter() - started) * 1000
        return result
    finally:
        result["request_ms"] = (time.perf_counter() - started) * 1000
    result["attempted"] = True
    result["http_status"] = response.status_code
    if response.status_code != 200:
        result["status"] = "HTTP_ERROR"
        return result
    try:
        body = response.json()
        result["usage"] = _observed_usage(body.get("usage"))
        sub_queries = parse_decomposition(body["choices"][0]["message"]["content"])
        result.update(status="VALID", sub_queries=list(sub_queries))
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        result["status"] = "INVALID_OUTPUT"
    return result


def tokenize(text: str) -> tuple[str, ...]:
    return tuple(_WORD.findall(text.lower()))


class BM25Index:
    """Deterministic lexical ranker over one question-local corpus."""

    def __init__(self, documents: Sequence[str], *, k1: float = 1.5, b: float = 0.75) -> None:
        if not documents:
            raise ValueError("BM25 requires a non-empty corpus")
        self.k1, self.b = k1, b
        self.doc_tokens = [tokenize(doc) for doc in documents]
        self.doc_lengths = [len(tokens) for tokens in self.doc_tokens]
        self.average_length = sum(self.doc_lengths) / len(self.doc_lengths)
        self.term_counts: list[dict[str, int]] = []
        document_frequency: dict[str, int] = {}
        for tokens in self.doc_tokens:
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self.term_counts.append(counts)
            for token in counts:
                document_frequency[token] = document_frequency.get(token, 0) + 1
        total = len(documents)
        self.idf = {
            token: ((total - df + 0.5) / (df + 0.5) + 1.0)
            for token, df in document_frequency.items()
        }

    def score(self, query: str, index: int) -> float:
        counts = self.term_counts[index]
        length = self.doc_lengths[index]
        total = 0.0
        for token in tokenize(query):
            if token not in counts:
                continue
            frequency = counts[token]
            idf = self.idf[token]
            denominator = frequency + self.k1 * (1 - self.b + self.b * length / self.average_length)
            total += idf * frequency * (self.k1 + 1) / denominator
        return total

    def ranking(self, query: str) -> list[int]:
        """Document indices from highest to lowest score, ties by index."""
        scores = [(self.score(query, index), index) for index in range(len(self.term_counts))]
        scores.sort(key=lambda item: (-item[0], item[1]))
        return [index for _, index in scores]


def rrf_fuse_n(rankings: Sequence[Sequence[int]], *, k: int = RRF_K) -> list[int]:
    """N-way reciprocal-rank fusion over document-index rankings.

    Same formula as the production ``fuse_reciprocal_rank`` (score
    1/(k+rank), rank starting at 1, ties by best source rank then id),
    generalized from two lists to N.
    """
    if not rankings:
        raise ValueError("fusion requires at least one ranking")
    scores: dict[int, float] = {}
    best_rank: dict[int, int] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
            best_rank[doc] = min(best_rank.get(doc, rank), rank)
    ordered = sorted(scores, key=lambda doc: (-scores[doc], best_rank[doc], doc))
    seen = set()
    for ranking in rankings:
        seen.update(ranking)
    return [doc for doc in ordered if doc in seen] or sorted(seen)


__all__ = [
    "BM25Index",
    "DECOMPOSE_SYSTEM_PROMPT",
    "MAX_SUB_QUERIES",
    "RRF_K",
    "decompose_messages",
    "decompose_once",
    "parse_decomposition",
    "rrf_fuse_n",
    "tokenize",
]
