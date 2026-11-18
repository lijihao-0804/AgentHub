"""Audit the observed Chinese refusals to the frozen English questions after unblinding."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import httpx

from .attempts import read_attempt, read_http
from .dataset import HERE, canonical_digest
from .summarize import deltas, metrics

ARMS = ("baseline", "candidate")
STAGES = ("first", "final")
CHINESE_REFUSALS = {"证据不足以回答此问题。", "证据不足，无法回答这个问题。"}
HAN = re.compile(r"[\u3400-\u9fff]")
LABELS = {True: "compatible", False: "mismatch", None: "unassessable"}


def language_match(answer):
    if not answer.strip():
        return None
    if answer.strip() in CHINESE_REFUSALS:
        return False
    if HAN.search(answer):
        raise ValueError("Another reply containing Han characters requires a language review.")
    return True


def index_rows(data, key, predicate=lambda row: True):
    items = {}
    for line in data.splitlines():
        row = json.loads(line)
        if predicate(row):
            name = key(row)
            if name in items:
                raise ValueError("Duplicate question-arm observation.")
            items[name] = row
    return items


def audit(run, cases_file, reviewed_file):
    cases_raw, reviewed_raw = cases_file.read_bytes(), reviewed_file.read_bytes()
    wire_raw = (run / "wire-private.jsonl").read_bytes()
    http_raw = (run / "http-private.jsonl").read_bytes()
    freeze = json.loads((run / "freeze.json").read_bytes())
    execution = json.loads((run / "execution.json").read_bytes())
    samples = sorted(json.loads(cases_raw)["cases"], key=lambda c: c["id"])
    wire_sha = hashlib.sha256(wire_raw).hexdigest()
    http_sha = hashlib.sha256(http_raw).hexdigest()
    if (
        execution["offline"]
        or freeze["offline"]
        or wire_sha != execution["wire_sha256"]
        or http_sha != execution["http_sha256"]
        or canonical_digest(samples) != freeze["cases_sha256"]
        or len({c["id"] for c in samples}) != 100
        or len(samples) != 100
        or sum(c["answerable"] for c in samples) != 50
        or any(HAN.search(c["question"]) for c in samples)
    ):
        raise ValueError("Expected the completed, frozen English campaign and original wire logs.")
    wire = index_rows(wire_raw, lambda r: r["key"], lambda r: r["attempt"] == 1)
    http = index_rows(http_raw, lambda r: r["key"], lambda r: r["state"] == "returned")
    reviewed = index_rows(reviewed_raw, lambda r: f"{r['id']}/{r['arm']}")
    expected = {f"{c['id']}/{arm}" for c in samples for arm in ARMS}
    if any(rows.keys() != expected for rows in (wire, http, reviewed)):
        raise ValueError("Missing or extra first-response, endpoint, or review record.")

    pairs, affected = {}, []
    counts = {arm: {s: Counter() for s in STAGES} for arm in ARMS}
    for case in samples:
        qid, doc = case["id"], case["doc_id"]
        details = {"id": qid, "question": case["question"]}
        mismatch = False
        pairs[qid] = {}
        for arm in ARMS:
            key = f"{qid}/{arm}"
            first, returned, row = wire[key], http[key], reviewed[key]
            if (
                case["answerable"] != row["answerable"]
                or returned["http"] != row["http"]
                or not first["messages"][1]["content"].startswith(
                    f"Question:\n{case['question']}\n"
                )
            ):
                raise ValueError("The review or first request refers to a different question.")
            results = {
                "first": read_attempt(first, returned["evidence"]),
                "final": read_http(
                    httpx.Response(returned["http"], json=returned["body"]),
                    freeze["inputs"][doc],
                    doc,
                ),
            }
            pairs[qid][arm] = row
            details[arm] = {}
            for stage, result in results.items():
                item = row[stage]
                answer = result["answer"]
                if (
                    result["state"] != item["state"]
                    or hashlib.sha256(answer.encode()).hexdigest() != item["answer_sha256"]
                    or len(result["citations"]) != item["cited_chunks"]
                ):
                    raise ValueError("The semantic judgment differs from its recorded reply.")
                match = language_match(answer)
                counts[arm][stage][LABELS[match]] += 1
                scores = item["scores"]
                joint = scores["joint_correct"]
                if joint != (scores["answer_correct"] and scores["citation_correct"]):
                    raise ValueError("The input does not contain the adjudicated joint score.")
                for field in ("answer_correct", "citation_correct", "joint_correct"):
                    scores[field] = scores[field] and match is True
                detail = {
                    "state": result["state"],
                    "language_matches_question": match,
                    "reviewed_semantic_and_citation_pass": joint,
                    "pass_with_language": scores["joint_correct"],
                    "review_note": item.get("semantic_review", {}).get("reason")
                    or item.get("invalid_reason"),
                }
                if item.get("invalid_reason") == "citations_on_refusal":
                    detail["review_note"] = "The first reply declared a refusal and cited evidence."
                if match is False:
                    if result["state"] != "refusal":
                        raise ValueError(
                            "Unexpected Chinese text outside the reviewed refusal state."
                        )
                    detail["answer"] = answer
                    mismatch = True
                details[arm][stage] = detail
        if mismatch:
            affected.append(details)

    arms, changes = {}, {}
    for arm in ARMS:
        side = [pair[arm] for pair in pairs.values()]
        arms[arm] = {
            s: {
                **metrics(side, s),
                "language_screen": {label: counts[arm][s][label] for label in LABELS.values()},
            }
            for s in STAGES
        }
    for stage in STAGES:
        changes[stage] = {}
        for label, field, positive in (
            ("strict", "joint_correct", None),
            ("positive_value", "answer_correct", True),
            ("positive_supported", "joint_correct", True),
            ("correct_refusal", "joint_correct", False),
        ):
            diff = deltas(pairs, stage, field, positive)
            changes[stage][label] = {
                "improved": diff["improved"],
                "regressed": diff["regressed"],
                "unchanged_count": diff["unchanged_count"],
                "net_count": len(diff["improved"]) - len(diff["regressed"]),
            }
    return {
        "scope": (
            "Post-unblinding supplement for the 100 frozen English questions. The preregistered "
            "rubric scored semantics and citations. Language is an additional criterion here."
        ),
        "language_screen": (
            "Two manually verified Chinese refusal sentences fail this English-question screen "
            "when they form the whole reply. Other visible answers are treated as compatible "
            "with English for this narrow check. A missing answer field is unassessable."
        ),
        "metric_definition": (
            "Value, support and joint success counts also require a language match. "
            "State counts and false refusals use the original definitions."
        ),
        "inputs_sha256": {
            "canonical_cases": freeze["cases_sha256"],
            "wire": wire_sha,
            "http": http_sha,
            "reviewed_file_lf": hashlib.sha256(reviewed_raw.replace(b"\r\n", b"\n")).hexdigest(),
        },
        "arms": arms,
        "paired_changes": changes,
        "mismatching_questions": affected,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=HERE / "cases.json")
    parser.add_argument("--reviewed", type=Path, default=HERE / "results/reviewed.jsonl")
    parser.add_argument("--output", type=Path, default=HERE / "results/language-audit.json")
    args = parser.parse_args()
    report = audit(args.run, args.cases, args.reviewed)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
