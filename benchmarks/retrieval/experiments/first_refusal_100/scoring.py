"""Frozen first-pass evidence checks. Free-form claims receive a separate blinded review."""

from __future__ import annotations

import re
import unicodedata

MARKER = re.compile(r"\[(\d+)\]")
MARKER_GROUP = re.compile(r"\[\d+\](?:\s*\[\d+\])*")
CANONICAL_REFUSAL = re.compile(
    r"(?:there is )?(?:insufficient|not enough) evidence"
    r"(?: to answer(?: (?:this|the) question)?(?: from (?:the )?(?:selected|provided)"
    r" (?:knowledge snapshot|evidence|information))?)?[.!]?",
    re.I,
)
ABSTENTION = re.compile(
    r"\b(?:insufficient|not enough|cannot|can't|unable to|does not|do not|doesn't|"
    r"not (?:provided|stated|specified|reported|given|available|mentioned)|no information)\b",
    re.I,
)


def norm(text: str) -> str:
    """Match tokens, not substrings, retaining decimal points between digits."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    deaccented = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    tokens = re.findall(r"\d+(?:\.\d+)?|[^\W\d_]+", deaccented)
    return " ".join(tokens)


def _has_alias(claim: str, aliases: list[str]) -> bool:
    hay = " " + norm(claim) + " "
    return any(" " + norm(alias) + " " in hay for alias in aliases)


def _collapse(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _citation_coverage(case: dict, cited: dict, document: str) -> set[int]:
    if cited.get("doc_id") != case["doc_id"] or not isinstance(cited.get("text"), str):
        return set()
    excerpt = _collapse(cited["text"])
    if not excerpt or excerpt not in _collapse(document):
        return set()
    return {
        i
        for i, facet in enumerate(case["facets"])
        if any(_collapse(document[start:end]) in excerpt for start, end in facet["support"])
    }


def evidence_covers(case: dict, evidence: list[dict]) -> bool:
    """Whether the actual context includes at least one frozen support span for each facet."""
    from .dataset import load_documents

    if not case["answerable"]:
        return False
    doc = load_documents()[case["doc_id"]]["text"]
    covered = set().union(*(_citation_coverage(case, c, doc) for c in evidence))
    return len(covered) == len(case["facets"])


def _linked_claims(answer: str) -> dict[int, list[str]]:
    by_id: dict[int, list[str]] = {}
    previous = 0
    for group in MARKER_GROUP.finditer(answer):
        preceding = answer[previous : group.start()].strip(" ,;:\n\t")
        for marker in MARKER.findall(group.group()):
            by_id.setdefault(int(marker), []).append(preceding)
        previous = group.end()
    return by_id


def score_case(case: dict, *, state: str, answer: str, citations: list[dict]) -> dict[str, bool]:
    """Score one actual attempt. An unknown state or an error cannot earn refusal credit.

    A support span includes the relation or quantity being asked about. Free answers,
    paraphrases, scope, polarity, numeric role assignment and additional factual claims
    are adjudicated under the accompanying blind review rules before publication.
    """
    from .dataset import load_documents

    expected_answer = case["answerable"]
    answer = answer if isinstance(answer, str) else ""
    refused = state == "refusal"
    linked = _linked_claims(answer)
    ids = [c.get("id") for c in citations]
    valid_ids = all(type(n) is int and n >= 1 for n in ids) and len(set(ids)) == len(ids)
    valid = bool(answer.strip()) and (
        (refused and not ids and not linked)
        or (state == "answer" and bool(ids) and valid_ids and set(ids) == set(linked))
    )
    answer_correct = citation_correct = False
    needs_review = False
    if valid and refused:
        needs_review = not bool(CANONICAL_REFUSAL.fullmatch(answer.strip()))
        answer_correct = citation_correct = not expected_answer and bool(ABSTENTION.search(answer))
    if valid and state == "answer":
        needs_review = True
        if expected_answer:
            facets = case["facets"]
            answer_correct = all(_has_alias(MARKER.sub("", answer), f["aliases"]) for f in facets)
            doc = load_documents()[case["doc_id"]]["text"]
            linked_support = []
            for cited in citations:
                covered = _citation_coverage(case, cited, doc)
                for claim in linked[cited["id"]]:
                    linked_support.append(
                        {i for i in covered if _has_alias(claim, facets[i]["aliases"])}
                    )
            last_marker_end = max(m.end() for m in MARKER.finditer(answer))
            citation_correct = (
                all(linked_support)
                and len(set.union(*linked_support)) == len(facets)
                and not norm(answer[last_marker_end:])
            )
    return {
        "valid": valid,
        "refused": refused,
        "answer_correct": answer_correct,
        "citation_correct": citation_correct,
        "joint_correct": answer_correct and citation_correct,
        "wrong_refusal": expected_answer and refused,
        "unsafe_answer": not expected_answer and state == "answer",
        "needs_review": needs_review,
    }
