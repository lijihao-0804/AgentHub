"""Publish only derived grades and reconcile first/final pairs with the protected archive."""

import argparse
import json
import math
import subprocess
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median

from .packets import GROUPS, ROOT, load, rows, save

ARCHIVE = "85563aebcc3f9e9ee302bec951fdff71a6977c2e"
PREFIX = "benchmarks/retrieval/experiments/bilingual_refusal_100/results/"


def protections():
    def prior(filename):
        return subprocess.check_output(["git", "show", f"{ARCHIVE}:{PREFIX}{filename}"], cwd=ROOT)

    passed = defaultdict(set)
    for name in ("protected-100.json", "protected-wai14.json"):
        for r in json.loads(prior(name))["rows"]:
            if r["previous_success"] or r["current"]:
                passed[r["stage"]].add(r["id"])
    for line in prior("reviewed-rows.jsonl").splitlines():
        r = json.loads(line)
        for stage in ("first", "final"):
            s = r[stage]["scores"]
            if s["semantic_and_citation_correct"] and s["language_match"]:
                passed[stage].add(r["id"])
    return passed


def grade(known, packet, judgment):
    if not all(type(judgment[k]) is bool for k in ("value", "support", "language")):
        raise ValueError("Expected three boolean judgments")
    value, support, language = (judgment[k] for k in ("value", "support", "language"))
    answerable = packet["gold"]["answerable"]
    valid, refused = packet["valid"], packet["refused"]
    if not valid and (value or support or language):
        raise ValueError("Missing or invalid output cannot earn credit")
    if refused and (support or (answerable and value) or packet["cited"]):
        raise ValueError("Refusal grade inconsistent with evidence state")
    if support and not packet["cited"]:
        raise ValueError("Claimed support without any actual citation")
    return dict(
        **known,
        valid=valid,
        refused=refused,
        answerable=answerable,
        value=value,
        support=support,
        language=language,
        strict=valid and value and language and (refused or support),
    )


def metrics(results):
    def tally(field, subset):
        return {"hits": sum(r[field] for r in subset), "total": len(subset)}

    asserted = [r for r in results if r["valid"] and not r["refused"]]
    negatives = [r for r in results if not r["answerable"]]
    answerable = [r for r in results if r["answerable"]]
    return {
        "content_correct": tally("value", results),
        "strict_correct": tally("strict", results),
        "correct_refusal": {
            "hits": sum(r["valid"] and r["value"] and r["refused"] for r in negatives),
            "total": len(negatives),
        },
        "wrong_refusal_on_answerable": tally("refused", answerable),
        "strict_correct_on_answerable": tally("strict", answerable),
        "citation_support": tally("support", asserted),
        "language_match": tally("language", results),
        "valid": tally("valid", results),
    }


def adjudicate(directory, packet_dir, judgments, ledger):
    packets = {r["uid"]: r for path in packet_dir.glob("blind-*.json") for r in load(path)}
    labels = {}
    for path in judgments:
        for r in load(path):
            if r["uid"] in labels:
                raise ValueError("Duplicate review")
            labels[r["uid"]] = r
    if labels.keys() != packets.keys():
        raise ValueError("Every blind packet needs exactly one final judgment")
    groups = defaultdict(list)
    derived = []
    for key in load(packet_dir / "keys.json"):
        row = grade(key, packets[key["uid"]], labels[key["uid"]])
        derived.append(row)
        groups[row["group"], row["arm"], row["stage"]].append(row)
    totals = {"/".join(key): metrics(group) for key, group in groups.items()}
    paired, historical = {}, {}
    protected = protections()
    for stage in ("first", "final"):
        base = {r["id"]: r for r in groups["fresh", "baseline", stage]}
        candidate = {r["id"]: r for r in groups["fresh", "candidate", stage]}
        common = sorted(base.keys() & candidate.keys())
        paired[stage] = dict(
            pairs=len(common),
            new_correct=[q for q in common if candidate[q]["strict"] and not base[q]["strict"]],
            lost_correct=[q for q in common if base[q]["strict"] and not candidate[q]["strict"]],
            lost_support_on_assertions=[
                q
                for q in common
                if base[q]["support"]
                and candidate[q]["valid"]
                and not candidate[q]["refused"]
                and not candidate[q]["support"]
            ],
            lost_correct_refusal=[
                q
                for q in common
                if not base[q]["answerable"]
                and base[q]["refused"]
                and base[q]["value"]
                and not (candidate[q]["refused"] and candidate[q]["value"])
            ],
        )
        regress = {
            r["id"]: r
            for r in derived
            if r["arm"] == "candidate"
            and r["group"] != "fresh"
            and r["stage"] == stage
            and r["id"] in protected[stage]
        }
        historical[stage] = dict(
            expected=len(protected[stage]),
            measured=len(regress),
            lost_correct=sorted(k for k, r in regress.items() if not r["strict"]),
        )
    events_by_id = {}
    for receipt in rows(ledger):
        if receipt.get("state") == "confirmed":
            if receipt["id"] in events_by_id:
                raise ValueError("Duplicate physical receipt in the formal ledger")
            events_by_id[receipt["id"]] = Decimal(receipt["estimated_cny"])
    exchanges = {}
    for group in GROUPS:
        path = directory / (group + ".jsonl")
        if not path.is_file():
            continue
        arms = defaultdict(list)
        for row in rows(path):
            arms[row["arm"]].append(row)
        for arm, logical in arms.items():
            elapsed = sorted(r["elapsed_ms"] for r in logical)
            events = [e for r in logical for e in r["events"]]
            amount = sum((events_by_id.pop(e["id"]) for e in events), Decimal(0))
            exchanges[group + "/" + arm] = dict(
                http=len(logical),
                calls=len(events),
                corrections=len(events) - len(logical),
                cny=str(amount),
                median_ms=round(median(elapsed)),
                p95_ms=round(elapsed[math.ceil(0.95 * len(elapsed)) - 1]),
                http_failures=[r["id"] for r in logical if r["http"] != 200],
            )
    coverage = load(packet_dir / "coverage.json")
    if not coverage["missing"] and events_by_id:
        raise ValueError("Confirmed receipt without a corresponding formal HTTP exchange")
    return dict(
        coverage=coverage,
        archive=ARCHIVE,
        unique_judgments=len(labels),
        cohorts=totals,
        exchanges=exchanges,
        fresh_pairs=paired,
        protected=historical,
        rows=derived,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("packets", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("judgments", type=Path, nargs="+")
    args = parser.parse_args()
    save(args.output, adjudicate(args.directory, args.packets, args.judgments, args.ledger))
