"""Frozen evidence-deletion diagnostics; labels are curated, not inferred by this code.

Exact source/identity preservation is mechanically checked. Source sufficiency
requires review; substring checks below are not a semantic entailment judge.
"""

from __future__ import annotations

import json
from statistics import fmean

from benchmarks.public_rag.attributed_reader_runner import from_wire
from benchmarks.public_rag.consumer_answer_metrics import answer_metrics
from benchmarks.public_rag.experimental.attributed_reader import (
    validate_reader_answer,
    visible_records,
)
from packages.model_gateway.contracts import ModelRequest

CONDITIONS = ("supported", "answer_evidence_removed")


def validate_pair(case):
    if set(case["conditions"]) != set(CONDITIONS):
        raise ValueError("both planned evidence conditions required")
    if not case["official_aliases"] or any(
        type(a) is not str or not a.strip() for a in case["official_aliases"]
    ):
        raise ValueError("original official aliases required")
    original, negative = (case["conditions"][c]["messages"] for c in CONDITIONS)
    for messages in (original, negative):
        if [m["content"] for m in messages if m["role"] == "user"] != [case["question"]]:
            raise ValueError("question differs between labeled pair and model input")
    index, payload, records = visible_records(ModelRequest(messages=from_wire(original)))
    negative_index, negative_payload, kept = visible_records(
        ModelRequest(messages=from_wire(negative))
    )
    if (
        index != negative_index
        or len(original) != len(negative)
        or any(
            a != b for i, (a, b) in enumerate(zip(original, negative, strict=True)) if i != index
        )
    ):
        raise ValueError("only the original evidence tool payload may change")
    if {k: v for k, v in original[index].items() if k != "content"} != {
        k: v for k, v in negative[index].items() if k != "content"
    }:
        raise ValueError("evidence tool identity must remain unchanged")
    removed = case["removed_source_ids"]
    known = {r["chunk_id"] for r in records}
    if (
        type(removed) is not list
        or not removed
        or any(type(v) is not str or v not in known for v in removed)
        or len(set(removed)) != len(removed)
        or len(removed) == len(known)
    ):
        raise ValueError("removal must name a nonempty proper subset of original sources")
    expected = {
        **payload,
        "data": {
            **payload["data"],
            "results": [r for r in records if r["chunk_id"] not in removed],
            "evidence_refs": [
                r for r in payload["data"]["evidence_refs"] if r["chunk_id"] not in removed
            ],
        },
    }
    if negative_payload != expected:
        raise ValueError("remaining source text/order/refs/lifecycle must be exactly preserved")
    if (
        case["conditions"][CONDITIONS[0]]["expected"] != "ANSWER"
        or case["conditions"][CONDITIONS[1]]["expected"] != "ABSTAIN_UNDER_SOURCE_ONLY_POLICY"
    ):
        raise ValueError("invalid condition labels")
    by_id = {r["chunk_id"]: r["snippet"] for r in records}
    facts = case["curation"]["facts"]
    if not facts:
        raise ValueError("curated supporting source spans required")
    for fact in facts:
        source, quote, start, end = (fact[k] for k in ("source_id", "quote", "start", "end"))
        if (
            source not in by_id
            or type(quote) is not str
            or not quote.strip()
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(by_id[source])
            or by_id[source][start:end] != quote
        ):
            raise ValueError("curation quote must match the exact original visible source span")
    if not any(f["source_id"] in removed for f in facts):
        raise ValueError("deleted evidence must include an explicitly curated required fact")
    return records, kept


def validate_dataset(dataset):
    if dataset.get("schema_version") != 1 or dataset.get("state") != "FROZEN_DEVELOPMENT":
        raise ValueError("one frozen development diagnostic dataset required")
    cases, ledger = dataset["cases"], dataset["ledger"]
    if not cases or len({c["query_id"] for c in cases}) != len(cases):
        raise ValueError("nonempty unique diagnostic pairs required")
    if len({r["query_id"] for r in ledger}) != len(ledger):
        raise ValueError("diagnostic ledger identities must be unique")
    ledger_by_id = {r["query_id"]: r for r in ledger}
    for case in cases:
        if case["query_id"] not in ledger_by_id:
            raise ValueError("active case must remain in the full diagnostic ledger")
        entry = ledger_by_id[case["query_id"]]
        if (
            case["question"] != entry["question"]
            or case["official_aliases"] != entry["official_aliases"]
        ):
            raise ValueError("curation cannot silently rewrite questions or official aliases")
        if case["annotation_state"] != "REVIEWED_SOURCE_CONTRAST":
            raise ValueError("model-only review labels cannot become active test labels")
        validate_pair(case)


def score_pairs(dataset, rows):
    validate_dataset(dataset)
    if [r["query_id"] for r in rows] != [c["query_id"] for c in dataset["cases"]]:
        raise ValueError("all planned pairs and failures must be retained in dataset order")
    positive, negative, paired = [], [], []
    for case, row in zip(dataset["cases"], rows, strict=True):
        if set(row["conditions"]) != set(CONDITIONS):
            raise ValueError("missing planned condition")
        for condition in CONDITIONS:
            api = row["conditions"][condition]["api"]
            if api["status"] == "VALID":
                _, _, records = visible_records(
                    ModelRequest(messages=from_wire(case["conditions"][condition]["messages"]))
                )
                validate_reader_answer(
                    json.dumps({k: api[k] for k in ("answer", "unanswerable", "cited_chunk_ids")}),
                    records,
                )
        a, b = (row["conditions"][c]["api"] for c in CONDITIONS)
        valid_a, valid_b = a["status"] == "VALID", b["status"] == "VALID"
        quality = answer_metrics(a.get("answer", "") if valid_a else "", case["official_aliases"])
        correct_refusal = (
            valid_b and b["unanswerable"] and b["answer"] == "" and b["cited_chunk_ids"] == []
        )
        unsupported = valid_b and not b["unanswerable"] and bool(b["answer"].strip())
        positive.append({**quality, "valid": valid_a, "abstained": valid_a and a["unanswerable"]})
        negative.append(
            {
                "valid": valid_b,
                "correct_refusal": correct_refusal,
                "unsupported_answer": unsupported,
            }
        )
        paired.append(
            {
                "query_id": case["query_id"],
                "supported": positive[-1],
                "answer_evidence_removed": negative[-1],
                "both_correct": bool(quality["exact_match"] and correct_refusal),
            }
        )
    n = len(rows)
    return {
        "scope": (
            "Curated exposed source-only deletion diagnostic; "
            "no population estimate or method adoption."
        ),
        "pairs": n,
        "planned_requests": n * 2,
        "ledger_cases": len(dataset["ledger"]),
        "unadjudicated_ledger_cases": len(dataset["ledger"]) - n,
        "supported": {
            "token_f1": fmean(r["token_f1"] for r in positive),
            "exact_match": fmean(r["exact_match"] for r in positive),
            "valid": sum(r["valid"] for r in positive),
            "abstained": sum(r["abstained"] for r in positive),
        },
        "answer_evidence_removed": {
            "valid": sum(r["valid"] for r in negative),
            "correct_refusals": sum(r["correct_refusal"] for r in negative),
            "correct_refusal_rate": sum(r["correct_refusal"] for r in negative) / n,
            "unsupported_answers": sum(r["unsupported_answer"] for r in negative),
            "unsupported_answer_rate": sum(r["unsupported_answer"] for r in negative) / n,
        },
        "both_correct": sum(r["both_correct"] for r in paired),
        "cases": paired,
        "limitations": [
            "Failures score0 and are not treated as correct refusals.",
            "Unsupported-answer rate counts only structurally valid delivered answers; "
            "invalid/provider outcomes reported separately.",
            "Small selected exposed set, deliberately ablated context; "
            "no representative accuracy or semantic-judge score.",
            "Labels describe sufficiency of supplied historical text, "
            "not answerability from the real world.",
        ],
    }
