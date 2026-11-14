"""Blind and score paired first HTTP responses; no model, credentials or network needed.

Run with --help. Receipts contain full public excerpts as returned by the API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from fractions import Fraction
from math import comb
from pathlib import Path

VERDICTS = {"full", "partial", "wrong", "refusal", "failure"}


def load(path):
    return json.loads(Path(path).read_bytes())


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf8")


def index(rows):
    indexed = {r["id"]: r for r in rows}
    if len(indexed) != len(rows):
        raise ValueError("Duplicate question ID")
    return indexed


def packets(cases, receipts):
    masked, keys = {}, {}
    for arm, returned in receipts.items():
        rows = index(returned)
        if set(cases) != set(rows):
            raise ValueError(f"Incomplete cohort for {arm}")
        keys[arm] = {}
        for cid, c in cases.items():
            r = rows[cid]
            item = {
                "question": c["query"],
                "negative": c.get("negative", False) or c.get("type") == "missing_evidence",
                "reference_answers": c.get("answers", []),
                "reference_evidence": c.get("evidence", []),
                "reference_rule": c.get("source_review_rule"),
                "gold_sources": c.get("gold_documents", []),
                "status": r["http"],
                "answer": r.get("answer"),
                "insufficient": r.get("insufficient"),
                "citations": [
                    {k: h[k] for k in ("number", "source", "excerpt")}
                    for h in r.get("citations", [])
                ],
            }
            uid = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:20]
            if uid in masked and masked[uid] != item:
                raise ValueError("Review identifier collision")
            masked[uid] = item
            keys[arm][cid] = uid
    return dict(sorted(masked.items())), keys


def assess(item, mark):
    verdict = mark["verdict"]
    good = mark["supporting_citations"]
    cites = [c["number"] for c in item["citations"]]
    if (
        verdict not in VERDICTS
        or not mark["reason"].strip()
        or not isinstance(good, list)
        or len(set(good)) != len(good)
        or not set(good) <= set(cites)
        or len(set(cites)) != len(cites)
    ):
        raise ValueError("Invalid review judgment")
    if item["status"] != 200:
        if verdict != "failure" or cites:
            raise ValueError("First HTTP error must count as a failure")
    elif item["insufficient"] is True:
        if verdict != "refusal" or cites:
            raise ValueError("Refusal must have no returned citations")
    elif verdict not in {"full", "partial", "wrong"} or not cites:
        raise ValueError("A positive response must be reviewed with its citations")
    if verdict in {"full", "partial"} and not good:
        raise ValueError("Grounded judgments require supporting evidence")
    return {
        **mark,
        "returned_citations": len(cites),
        "http": item["status"],
        "negative": item["negative"],
    }


def summarize(details, cases):
    arms = ("baseline", "candidate")
    totals = {}
    for scope in sorted({c["scope"] for c in cases.values()}):
        subset = {cid: d for cid, d in details.items() if cases[cid]["scope"] == scope}
        entry = {}
        metrics = {}
        for arm in arms:
            rows = [d[arm] for d in subset.values()]
            metrics[arm] = {
                "n": len(rows),
                "http200": sum(r["http"] == 200 for r in rows),
                "answerable": sum(not r["negative"] for r in rows),
                "unanswerable": sum(r["negative"] for r in rows),
                "positive_outcomes": dict(Counter(r["verdict"] for r in rows if not r["negative"])),
                "negative_outcomes": dict(Counter(r["verdict"] for r in rows if r["negative"])),
                "supporting_citations": sum(len(r["supporting_citations"]) for r in rows),
                "returned_citations": sum(r["returned_citations"] for r in rows),
            }
        entry["metrics"] = metrics
        for name, criterion in (
            ("http200", lambda r: r["http"] == 200),
            ("supported_answer", lambda r: not r["negative"] and r["verdict"] == "full"),
            ("correct_refusal", lambda r: r["negative"] and r["verdict"] == "refusal"),
            (
                "wrong_positive",
                lambda r: (
                    r["verdict"] == "wrong"
                    or (r["negative"] and r["verdict"] in {"full", "partial"})
                ),
            ),
        ):
            old, new = ({cid for cid, d in subset.items() if criterion(d[a])} for a in arms)
            entry[name] = {
                "retained": sorted(old & new),
                "lost": sorted(old - new),
                "gained": sorted(new - old),
            }
        totals[scope] = entry
    return totals


def decision(summary, cohort="independent"):
    # Fixed gate for the untouched paired cohort, plus separate historical preservation checks.
    fresh = summary[cohort]
    old, new = (fresh["metrics"][arm] for arm in ("baseline", "candidate"))
    up, down = len(fresh["http200"]["gained"]), len(fresh["http200"]["lost"])
    n = up + down
    # Small cohorts: show the two-sided exact paired sign test without calling direction a proof.
    p = min(1, 2 * sum(comb(n, j) for j in range(min(up, down) + 1)) / 2**n)

    def noninferior_citations(a, b):
        if not a["returned_citations"]:
            return None
        if not b["returned_citations"]:
            return False
        return Fraction(b["supporting_citations"], b["returned_citations"]) >= Fraction(
            a["supporting_citations"], a["returned_citations"]
        )

    checks = {
        "http_200_improved": new["http200"] > old["http200"],
        "previously_correct_preserved": not fresh["supported_answer"]["lost"],
        "previously_correct_refusals_preserved": not fresh["correct_refusal"]["lost"],
        "no_new_wrong_positive": not fresh["wrong_positive"]["gained"],
        "semantic_citation_precision_preserved": noninferior_citations(old, new),
    }
    historical = {
        s: {
            "lost_supported": v["supported_answer"]["lost"],
            "lost_correct_refusals": v["correct_refusal"]["lost"],
            "new_wrong_positives": v["wrong_positive"]["gained"],
        }
        for s, v in summary.items()
        if s != cohort
    }
    return {
        "independent_checks": checks,
        "historical_preservation": historical,
        "http_discordant_gain_loss": [up, down],
        "two_sided_exact_p_http": p,
        "note": (
            "Source integrity, retrieval parity and historical controls are checked separately."
        ),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("protocol", type=Path)
    for name in ("baseline", "candidate"):
        p.add_argument("--" + name, type=Path, nargs="+", required=True)
    p.add_argument("--packet", type=Path)
    p.add_argument("--judgments", type=Path)
    p.add_argument("--report", type=Path)
    args = p.parse_args()
    cases = index(load(args.protocol)["cases"])
    masked, keys = packets(
        cases,
        {
            a: [row for receipt in getattr(args, a) for row in load(receipt)["rows"]]
            for a in ("baseline", "candidate")
        },
    )
    if args.packet:
        save(args.packet, masked)
    if bool(args.judgments) != bool(args.report) or not (args.packet or args.report):
        p.error("write a packet and/or supply both judgments and a report destination")
    if args.report:
        marks = load(args.judgments)
        if set(marks) != set(masked):
            raise ValueError("Incomplete or stale blinded judgments")
        details = {
            cid: {
                a: {"masked_id": ids[cid], **assess(masked[ids[cid]], marks[ids[cid]])}
                for a, ids in keys.items()
            }
            for cid in cases
        }
        summary = summarize(details, cases)
        # Prespecified exposure sensitivity: this ID was reserved but never previously run.
        reserved = "09f0dce416a1e40cc6a24a8b42a802747d2c9363"
        cohort = "independent" if "independent" in summary else "reserve14"
        result = {"summary": summary, "assessment": decision(summary, cohort), "rows": details}
        if reserved in cases and cases[reserved]["scope"] == "independent":
            keep = {
                cid: c
                for cid, c in cases.items()
                if cid != reserved and c["scope"] == "independent"
            }
            sensitivity = summarize({cid: details[cid] for cid in keep}, keep)
            result["excluding_previously_reserved"] = {
                "excluded": reserved,
                "summary": sensitivity,
                "assessment": decision(sensitivity),
            }
        save(args.report, result)


if __name__ == "__main__":
    main()
