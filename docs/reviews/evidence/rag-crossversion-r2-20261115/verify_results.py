"""Read-only, standard-library verification of retained derived RAG results.

No provider, database, source dataset, credentials or network access is required.
This recomputes locked classifications, not a new semantic assessment.
"""

import csv
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    root = Path(__file__).parent
    manifest = read(root / "retained-manifest.json")
    for name, expected in manifest["files"].items():
        path = root / name
        if path.suffix == ".csv":
            with path.open(encoding="utf-8", newline="") as handle:
                data = list(csv.DictReader(handle))
        else:
            data = read(path)
        encoded = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        assert hashlib.sha256(encoded).hexdigest() == expected, name
    with (root / "question-failure-matrix.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 48
    assert len({(r["id"], r["arm"]) for r in rows}) == 48
    summary = read(root / "paired-summary.json")
    scores = {}
    for arm in "ABCD":
        subset = [r for r in rows if r["arm"] == arm]
        assert len(subset) == 12 and all(r["unknown"] == "False" for r in subset)
        scores[arm] = {r["id"]: r["complete_correct"] == "True" for r in subset}
        assert sum(scores[arm].values()) == summary["groups"][arm]["complete_correct"]
        for field in (
            "physical_requests",
            "input_tokens",
            "output_tokens",
            "estimated_or_reserved_microcny",
        ):
            assert sum(int(r[field]) for r in subset) == summary["groups"][arm][field]
    for name, comparison in summary["comparisons"].items():
        candidate, baseline = name.split("_vs_")
        assert scores[candidate].keys() == scores[baseline].keys()
        differences = [int(v) - int(scores[baseline][k]) for k, v in scores[candidate].items()]
        assert sum(d > 0 for d in differences) == comparison["wins"]
        assert sum(d == 0 for d in differences) == comparison["ties"]
        assert sum(d < 0 for d in differences) == comparison["losses"]
    assert sum(int(r["physical_requests"]) for r in rows) == 108
    assert sum(int(r["estimated_or_reserved_microcny"]) for r in rows) == 946582
    assert summary["cost"]["new_microcny"] == 946582 + 7940
    assert summary["cost"]["cumulative_microcny"] == 954522 + 968524
    persisted = read(root / "real-postgres-reconciliation.json")
    assert len(persisted["rows"]) == 12
    assert all(r["receipt_totals_match"] for r in persisted["rows"])
    assert sum(r["selection_rows"] for r in persisted["rows"]) == 24
    assert sum(r["auxiliary_rows"] for r in persisted["rows"]) == 24
    print(
        json.dumps(
            {
                "status": "PASS",
                "semantic_hashes": len(manifest["files"]),
                "locked_rows": len(rows),
                "complete_correct": {a: sum(s.values()) for a, s in scores.items()},
                "formal_physical_requests": 108,
                "canary_physical_requests": 4,
                "cumulative_estimated_or_reserved_microcny": 1923046,
                "new_network_or_model_calls": 0,
                "files_written": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
