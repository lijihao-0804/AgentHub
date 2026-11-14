"""Reconcile new article-selection evidence with the previous per-query baseline."""

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLD = HERE.parent / "candidate-recall-20261114" / "portable" / "hits.jsonl"
FIELDS = ("source20", "source5", "evidence20", "evidence5")
FUSION_FIELDS = ("sources20", "sources5", "rationales20", "rationales5")


def read(path):
    return json.loads(path.read_bytes())


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def check(condition, message):
    if not condition:
        raise ValueError(message)


def layers(method):
    return [method[key] for key in FIELDS]


def main():
    historical = rows(OLD)
    fusion = rows(HERE / "fusion/per-query.jsonl")
    parents = read(HERE / "parent/cases.json")
    doc_report = read(HERE / "fusion/report.json")
    parent_report = read(HERE / "parent/report.json")
    for file, digest in (
        ("fusion/per-query.jsonl", doc_report["per_query_sha256"]),
        ("parent/cases.json", parent_report["case_sha256"]),
        ("parent/six.json", parent_report["six_sha256"]),
    ):
        actual = hashlib.sha256((HERE / file).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        check(actual == digest, f"Changed recorded rows: {file}")
    queries = [r["query"] for r in historical]
    check(len(set(queries)) == len(queries) == 681, "Incomplete or duplicate baseline")
    check(
        [r["query_id"] for r in fusion] == queries and set(queries) == set(parents),
        "Query set or order differs",
    )
    doc_fields = ("article_refill4", "article_first", "article_local_passage")
    parent_fields = ("title_parent4", "full_parent4")
    by_cohort = {}
    for hist, doc in zip(historical, fusion, strict=True):
        q = hist["query"]
        parent = parents[q]
        check(hist["gold"] == doc["gold"] == parent["gold"], f"Judgment mismatch {q}")
        check(
            hist["annotated"] == doc["annotated"] == parent["annotated"], f"Rationale mismatch {q}"
        )
        baseline = hist["baseline"]
        check(
            baseline == doc["baseline"]["layers"] == layers(parent["methods"]["baseline"]),
            f"Baseline mismatch {q}",
        )
        arms = {
            "baseline": baseline,
            **{a: doc[a]["layers"] for a in doc_fields},
            **{a: layers(parent["methods"][a]) for a in parent_fields},
        }
        totals = by_cohort.setdefault(hist["cohort"], {})
        for arm, observed in arms.items():
            values = totals.setdefault(arm, [[0, 0, 0] for _ in FIELDS])
            for i, (old, new) in enumerate(zip(baseline, observed, strict=True)):
                old, new = set(old), set(new)
                check(new <= set(hist["gold"] if i < 2 else hist["annotated"]), f"Unjudged hit {q}")
                values[i][0] += len(new)
                values[i][1] += len(new - old)
                values[i][2] += len(old - new)
    for cohort, totals in by_cohort.items():
        d = doc_report["cohorts"][
            "development" if cohort == "development" else "historical_external_by_group"
        ]
        p = parent_report["cohorts"][
            "development" if cohort == "development" else "previously_exposed_553"
        ]
        for arm, values in totals.items():
            for i, actual in enumerate(values):
                if arm in parent_fields:
                    data = p["methods"][arm]["stages"][FIELDS[i]]
                    expected = [data[k] for k in ("visible_pairs", "gained", "lost")]
                else:
                    data = d["arms"][arm][FUSION_FIELDS[i]]
                    expected = [data[k] for k in ("candidate", "gained", "lost")]
                check(actual == expected, f"Published totals differ for {cohort}/{arm}/{FIELDS[i]}")
            columns = [f"{value} (+{gain}/-{loss})" for value, gain, loss in values]
            print(f"{cohort:14} {arm:23} " + "  ".join(columns))
    print(
        "Verified 681 queries. Columns: sources@20, sources@5, "
        "complete rationales@20, complete rationales@5."
    )


if __name__ == "__main__":
    main()
