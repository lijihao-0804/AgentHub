"""Post-hoc single-refill upper bound, keeping the observed merge and all other evidence."""

import json
from collections import defaultdict

from .freeze import HERE, OUT, ROOT, dump, rows
from .score import covered, spans


def main():
    cases = {c["id"]: c for c in json.loads((OUT / "matched.json").read_bytes())["cases"]}
    actual = {p["id"]: p for p in json.loads((HERE / "primary.json").read_bytes())["pairs"]}
    report = []
    missed_without_slot = []
    for row in rows(ROOT / ".scratch/composite-qa-preflight/fresh.jsonl"):
        if row["arm"] != "candidate":
            continue
        ident = row["id"]
        observed = actual[ident]
        if not observed["eligible"]:
            if observed["pool_full"] == 2 and len(observed["baseline"]["full"]) < 2:
                missed_without_slot.append(ident)
            continue
        ranked, selected = row["ranked"], row["selected"]
        assert len(selected) == 6 and observed["candidate"]["composite"]
        case = cases[ident]
        source = {
            p["source_id"]: (ROOT / p["path"]).read_bytes().decode() for p in case["paragraphs"]
        }
        gold = {g["source_id"] for g in case["gold_spans"]}
        old_sources = {e["source_id"] for e in ranked[:6]}
        options = [r for r in ranked[6:20] if r["source_id"] not in old_sources]
        assert options[0]["chunk_id"] == selected[-1]["chunk_id"]
        options_by_full = defaultdict(list)
        options_by_visible = defaultdict(list)
        for option in options:
            candidate = selected[:-1] + [option]
            presented = spans(candidate, source)
            full = sum(covered(presented.get(g, ()), 0, len(source[g]), source[g]) for g in gold)
            options_by_full[full].append(option["chunk_id"])
            options_by_visible[len(gold.intersection(presented))].append(option["chunk_id"])
        report.append(
            dict(
                id=ident,
                legal_options=len(options),
                best_full=max(options_by_full),
                first_full=len(observed["candidate"]["full"]),
                best_visible=max(options_by_visible),
                first_visible=len(observed["candidate"]["visible"]),
            )
        )
    dump(
        HERE / "posthoc.json", dict(eligible=report, pool_complete_but_no_slot=missed_without_slot)
    )
    print("eligible", len(report), "without_slot", len(missed_without_slot))
    for metric in ("visible", "full"):
        first = [r["first_" + metric] for r in report]
        best = [r["best_" + metric] for r in report]
        print(metric, sum(first), sum(best), first.count(2), best.count(2))


if __name__ == "__main__":
    main()
