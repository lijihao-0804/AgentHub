"""Deterministic answer and citation diagnostics for public RAG consumers.

The answer scorer follows the normal SQuAD/MuSiQue English normalization and
exact-match/F1 rules.  Unlike the upstream scorer, two empty normalized answers
do not receive credit: an empty prediction is always a miss when a reference
exists, and an empty reference is rejected before scoring.

Citation metrics only check whether cited IDs exist in the admitted context.
They do not measure semantic support or faithfulness.
"""

from __future__ import annotations

import re
import string
from collections import Counter
from collections.abc import Sequence

type AnswerMetrics = dict[str, float | int]
type CitationMetrics = dict[str, float | int | bool]

_ARTICLES = re.compile(r"\b(?:a|an|the)\b", re.UNICODE)
_ASCII_PUNCTUATION = str.maketrans("", "", string.punctuation)


def answer_metrics(prediction: str, references: list[str]) -> AnswerMetrics:
    """Return exact match and maximum token F1 over English answer aliases.

    Normalization lowercases text, removes ASCII punctuation and whole-word
    ``a``/``an``/``the``, then collapses whitespace.  Token F1 is calculated
    from a ``Counter`` multiset intersection, so duplicate tokens matter.

    A missing prediction or a missing/empty reference collection is invalid.
    An empty prediction or a reference that normalizes to no tokens scores
    zero.  When both sides normalize to no tokens, both metrics also score
    zero; this intentionally differs from the upstream SQuAD helper, which
    gives two empty token lists F1 1.
    """

    if not isinstance(prediction, str):
        raise TypeError("prediction must be a string")
    if references is None:
        raise TypeError("references must be a non-empty list of strings")
    if not isinstance(references, Sequence) or isinstance(references, (str, bytes)):
        raise TypeError("references must be a non-empty list of strings")
    if not references:
        raise ValueError("references must contain at least one answer alias")
    if any(not isinstance(reference, str) for reference in references):
        raise TypeError("every reference must be a string")
    if any(not reference.strip() for reference in references):
        raise ValueError("references must not contain empty answer aliases")

    prediction_tokens = _answer_tokens(prediction)
    reference_token_lists = [_answer_tokens(reference) for reference in references]
    exact_match = max(
        int(bool(prediction_tokens) and prediction_tokens == reference_tokens)
        for reference_tokens in reference_token_lists
    )
    token_f1 = max(
        _token_f1(prediction_tokens, reference_tokens) for reference_tokens in reference_token_lists
    )
    return {"exact_match": exact_match, "token_f1": token_f1}


def citation_metrics(
    cited_chunk_ids: list[str],
    admitted_chunk_ids: list[str],
) -> CitationMetrics:
    """Measure citation ID validity without claiming semantic support.

    ``validity`` is the share of cited entries whose ID appears in the
    admitted set.  ``unknown`` counts cited entries absent from that set and
    ``duplicates`` counts repeated occurrences after the first.  With no
    citations, ``has_citations`` is false and ``validity`` is 0; it must not be
    interpreted as perfect faithfulness.
    """

    _require_string_list(cited_chunk_ids, field="cited_chunk_ids")
    _require_string_list(admitted_chunk_ids, field="admitted_chunk_ids")

    admitted = set(admitted_chunk_ids)
    seen: set[str] = set()
    duplicates = 0
    for chunk_id in cited_chunk_ids:
        if chunk_id in seen:
            duplicates += 1
        seen.add(chunk_id)

    unknown = sum(chunk_id not in admitted for chunk_id in seen)
    return {
        "validity": (len(seen) - unknown) / len(seen) if seen else 0.0,
        "unknown": unknown,
        "duplicates": duplicates,
        "has_citations": bool(seen),
    }


def _answer_tokens(answer: str) -> list[str]:
    normalized = answer.lower().translate(_ASCII_PUNCTUATION)
    normalized = _ARTICLES.sub(" ", normalized)
    return normalized.split()


def _token_f1(prediction: list[str], reference: list[str]) -> float:
    if not prediction or not reference:
        return 0.0
    common = Counter(prediction) & Counter(reference)
    matching_tokens = sum(common.values())
    if matching_tokens == 0:
        return 0.0
    precision = matching_tokens / len(prediction)
    recall = matching_tokens / len(reference)
    return 2 * precision * recall / (precision + recall)


def _require_string_list(value: object, *, field: str) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list of strings")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{field} must contain only non-empty strings")


__all__ = ["answer_metrics", "citation_metrics"]
