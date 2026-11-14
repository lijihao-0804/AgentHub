"""Verify article-fusion aggregate counts from published rows with the stdlib alone."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "results/published"
OUT = ROOT / "article-recall-20261114/fusion"
LAYERS = ("sources20", "sources5", "rationales20", "rationales5")


def check(condition, message):
    if not condition:
        raise ValueError(message)


def verify(results: Path = OUT):
    report = json.loads((results / "report.json").read_bytes())
    for name, digest in (
        ("protocol.json", "protocol_sha256"),
        ("per-query.jsonl", "per_query_sha256"),
    ):
        directory = OUT if name == "protocol.json" else results
        check(hashlib.sha256((directory / name).read_bytes()).hexdigest() == report[digest], name)
    portable = [
        json.loads(line)
        for line in (ROOT / "candidate-recall-20261114/portable/hits.jsonl")
        .read_bytes()
        .splitlines()
    ]
    rows = [json.loads(line) for line in (results / "per-query.jsonl").read_bytes().splitlines()]
    check(len(rows) == 681 and len({r["query_id"] for r in rows}) == 681, "cohort size")
    cohorts = {"development": [], "historical_external_by_group": [], "prior_misses": []}
    for original, r in zip(portable, rows, strict=True):
        check(
            r["query_id"] == original["query"] and r["baseline"]["layers"] == original["baseline"],
            "baseline",
        )
        check(
            r["gold"] == original["gold"] and r["annotated"] == original["annotated"], "judgments"
        )
        group = (
            "development" if original["cohort"] == "development" else "historical_external_by_group"
        )
        cohorts[group].append(r)
        if group == "development" and not original["baseline"][0]:
            cohorts["prior_misses"].append(r)
    for name, selected in cohorts.items():
        saved = report["cohorts"][name]
        check(len(selected) == saved["questions"], "questions")
        for arm, stats in saved["arms"].items():
            for i, layer in enumerate(LAYERS):
                old = [{*r["baseline"]["layers"][i]} for r in selected]
                new = [{*r[arm]["layers"][i]} for r in selected]
                for key, values in (
                    ("baseline", old),
                    ("candidate", new),
                    ("retained", [a & b for a, b in zip(old, new, strict=True)]),
                    ("gained", [b - a for a, b in zip(old, new, strict=True)]),
                    ("lost", [a - b for a, b in zip(old, new, strict=True)]),
                ):
                    check(sum(map(len, values)) == stats[layer][key], f"{name}/{arm}/{layer}/{key}")
                for key, tally in (
                    ("any_old", sum(map(bool, old))),
                    ("any_new", sum(map(bool, new))),
                    (
                        "any_gained",
                        sum(bool(b) and not a for a, b in zip(old, new, strict=True) if b),
                    ),
                    (
                        "any_lost",
                        sum(bool(a) and not b for a, b in zip(old, new, strict=True) if a),
                    ),
                ):
                    check(tally == stats[layer][key], f"{name}/{arm}/{layer}/{key}")
    print("681 baseline rows, 3 cohorts, 4 arms and 4 layers verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=OUT)
    verify(parser.parse_args().results)
