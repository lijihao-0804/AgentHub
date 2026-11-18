"""Frozen bilingual screen for content, the evidence actually cited, and reply language."""

from __future__ import annotations

import re
import unicodedata

from benchmarks.retrieval.experiments.first_refusal_100.scoring import (
    ABSTENTION,
    CANONICAL_REFUSAL,
    MARKER,
    _citation_coverage,
    _linked_claims,
)

HAN_RANGE = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
HAN = re.compile(f"[{HAN_RANGE}]")
TOKENS = re.compile(rf"[{HAN_RANGE}]|\d+(?:\.\d+)?|[^\W\d_{HAN_RANGE}]+")
ENGLISH_WORD = re.compile(r"[a-z]+", re.I)
ENGLISH_CLAUSE = re.compile(
    r"\b(?:is|are|was|were|has|have|had|according to|the passage|the document|"
    r"the evidence|the answer|it does|it did|cannot|insufficient|not enough)\b",
    re.I,
)
ZH_CLAUSE = re.compile(
    r"根据|依据|文中|材料|资料|文档|证据|没有|未(?:给|提|说|列|披)|无法|答案|是|为|于"
)
ZH_CANONICAL = re.compile(
    r"(?:证据不足(?:以回答(?:此|这个|该|这一)?问题)?|"
    r"(?:证据|材料|信息)不足[，,]\s*无法回答(?:此|这个|该|这一)?(?:问题)?|"
    r"没有足够(?:的)?(?:证据|信息)(?:来|以)?回答(?:此|这个|该|这一)?(?:问题)?)[。.!！]?"
)
ZH_ABSTENTION = re.compile(
    r"不足|无法|不能(?:确定|得知|判断|回答)|不(?:清楚|知道)|"
    r"(?:未|没有|并无|并未)(?:明[确示]|给出|说明|介绍|交代|披露|列出|提到|提及|提供|记载|记录|指出|包含)"
)


def norm(value: str) -> str:
    """Keep alphanumeric word boundaries and index Han characters without a word dictionary."""
    raw = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(TOKENS.findall("".join(c for c in raw if not unicodedata.combining(c))))


def has_alias(text: str, aliases: list[str]) -> bool:
    words = " " + norm(text) + " "
    return any(" " + norm(alias) + " " in words for alias in aliases)


def language_match(case: dict, answer: str) -> bool:
    """Provisional script/clause screen. Bilingual adjudication resolves ambiguous free text."""
    plain = MARKER.sub("", answer).strip()
    if not plain:
        return False
    if norm(plain) in {norm(s) for s in case.get("neutral_aliases", ())}:
        return True
    han = bool(HAN.search(plain))
    latin = bool(ENGLISH_WORD.search(plain))
    if not han or not latin:
        return han if case["language"] == "zh" else latin
    zh_clause, en_clause = bool(ZH_CLAUSE.search(plain)), bool(ENGLISH_CLAUSE.search(plain))
    if zh_clause != en_clause:
        return zh_clause if case["language"] == "zh" else en_clause
    if case["language"] == "zh":
        return han and not en_clause
    return latin and en_clause and not zh_clause


def evidence_covers(case: dict, evidence: list[dict]) -> bool:
    from .dataset import load_documents

    if not case["answerable"]:
        return False
    document = load_documents()[case["doc_id"]]["text"]
    covered = set().union(*(_citation_coverage(case, item, document) for item in evidence))
    return len(covered) == len(case["facets"])


def score_case(case: dict, *, state: str, answer: str, citations: list[dict]) -> dict[str, bool]:
    from .dataset import load_documents

    answer = answer if isinstance(answer, str) else ""
    refused = state == "refusal"
    expected = case["answerable"]
    linked = _linked_claims(answer)
    ids = [c.get("id") for c in citations]
    id_valid = all(type(n) is int and n > 0 for n in ids) and len(set(ids)) == len(ids)
    valid = bool(answer.strip()) and (
        (refused and not ids and not linked)
        or (state == "answer" and bool(ids) and id_valid and set(ids) == set(linked))
    )
    compatible = language_match(case, answer)
    answer_correct = citation_correct = needs_review = False
    if valid and refused:
        canonical = bool(
            CANONICAL_REFUSAL.fullmatch(answer.strip()) or ZH_CANONICAL.fullmatch(answer.strip())
        )
        needs_review = not canonical
        abstains = canonical or bool(ABSTENTION.search(answer) or ZH_ABSTENTION.search(answer))
        answer_correct = citation_correct = not expected and abstains
    if valid and state == "answer":
        needs_review = True
        if expected:
            facets = case["facets"]
            answer_correct = all(has_alias(MARKER.sub("", answer), f["aliases"]) for f in facets)
            document = load_documents()[case["doc_id"]]["text"]
            attached = []
            for cite in citations:
                coverage = _citation_coverage(case, cite, document)
                for claim in linked[cite["id"]]:
                    attached.append({i for i in coverage if has_alias(claim, facets[i]["aliases"])})
            last_end = max(m.end() for m in MARKER.finditer(answer))
            citation_correct = (
                all(attached)
                and len(set.union(*attached)) == len(facets)
                and not norm(answer[last_end:])
            )
    return {
        "valid": valid,
        "refused": refused,
        "answer_correct": answer_correct,
        "citation_correct": citation_correct,
        "joint_correct": answer_correct and citation_correct and compatible,
        "wrong_refusal": expected and refused,
        "unsafe_answer": not expected and state == "answer",
        "needs_review": needs_review,
        "language_match": compatible,
    }
