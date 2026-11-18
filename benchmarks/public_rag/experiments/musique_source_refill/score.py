"""Audit candidate and prompt coverage of both official supporting paragraphs."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from .freeze import HERE, OUT, ROOT, dump, rows, sha


def spans(records, sources):
    result = defaultdict(list)
    for record in records:
        src, text, locator = record["source_id"], record["text"], record["locator"]
        start, end = locator["char_start"], locator["char_end"]
        raw = sources[src][start:end]
        if raw.strip() != text:
            raise ValueError(f"Evidence text/locator mismatch: {src}")
        result[src].append(
            (start + len(raw) - len(raw.lstrip()), end - len(raw) + len(raw.rstrip()))
        )
    return result


def covered(intervals, start, end, raw):
    pos = start
    for left, right in sorted(intervals):
        if left > pos and raw[pos : min(left, end)].strip():
            break
        pos = max(pos, right)
        if pos >= end:
            return True
    return not raw[pos:end].strip()


def measure(case, arms):
    sources = {p["source_id"]: (ROOT / p["path"]).read_bytes().decode() for p in case["paragraphs"]}
    gold = {span["source_id"] for span in case["gold_spans"]}
    a, b = arms["baseline"], arms["candidate"]

    def ids(records):
        return [item["chunk_id"] for item in records]

    assert ids(a["ranked"][:6]) == ids(b["ranked"][:6]) and len(b["ranked"]) <= 20
    if "body" in a:
        for stage in ("dense", "sparse", "fused", "selection"):
            stages = [arm["body"]["retrieval_trace"]["stages"][stage] for arm in (a, b)]
            assert ids(stages[0]["results"]) == ids(stages[1]["results"])
        fused = b["body"]["retrieval_trace"]["stages"]["fused"]["results"]
        assert set(ids(fused)) == set(ids(b["ranked"]))
    else:
        assert ids(a["ranked"]) == ids(b["ranked"])
    pool = spans(b["ranked"], sources)
    result = dict(
        id=case["id"],
        pool_n=len(b["ranked"]),
        pool_gold=len(gold & pool.keys()),
        pool_full=sum(covered(pool[g], 0, len(sources[g]), sources[g]) for g in gold),
        rank_sha256=sha("|".join(ids(b["ranked"]))),
    )
    from packages.knowledge.contracts import RetrievedEvidence
    from packages.knowledge.source_merge import propose_source_refill

    def hydrate(r):
        s = r["source_id"]
        return RetrievedEvidence(
            s, r.get("revision", s), r["chunk_id"], s, r["locator"], r["text"], 0.0, None, {}
        )

    result["eligible"] = propose_source_refill(tuple(map(hydrate, b["ranked"]))) is not None
    old = None
    for name in ("baseline", "candidate"):
        arm = arms[name]
        prompt = arm["events"][0]["messages"][1]["content"]
        for r in arm["selected"]:
            assert r["text"] in prompt
        shown = spans(arm["selected"], sources)
        if old is not None:
            for s, ranges in old.items():
                assert all(covered(shown[s], left, right, sources[s]) for left, right in ranges)
        old = shown
        result[name] = dict(
            visible=sorted(gold & {s for s, intervals in shown.items() if intervals}),
            full=sorted(g for g in gold if covered(shown[g], 0, len(sources[g]), sources[g])),
            sources=sum(bool(v) for v in shown.values()),
            prompt_sha256=sha(prompt),
            composite=any(r["locator"]["type"] == "source_union" for r in arm["selected"]),
        )
    return result


def aggregate(items):
    result = dict(
        questions=len(items),
        gold_sources=2 * len(items),
        eligible=sum(x["eligible"] for x in items),
        adopted=sum(x["candidate"]["composite"] for x in items),
        pool_gold=sum(x["pool_gold"] for x in items),
        pool_full=sum(x["pool_full"] for x in items),
        pool_complete_questions=sum(x["pool_full"] == 2 for x in items),
    )
    for metric in ("visible", "full"):
        for arm in ("baseline", "candidate"):
            result[f"{arm}_{metric}"] = sum(len(x[arm][metric]) for x in items)
            result[f"{arm}_both_{metric}"] = sum(len(x[arm][metric]) == 2 for x in items)
        for left, right, name in (
            ("candidate", "baseline", "gain"),
            ("baseline", "candidate", "loss"),
        ):
            result[f"{name}_{metric}"] = sum(
                len(set(x[left][metric]) - set(x[right][metric])) for x in items
            )
            result[f"{name}_both_{metric}"] = sum(
                len(x[left][metric]) == 2 and len(x[right][metric]) < 2 for x in items
            )
    return result


def main(path, label):
    cases = json.loads((OUT / "matched.json").read_bytes())["cases"]
    received = defaultdict(dict)
    for row in rows(path):
        assert row["arm"] not in received[row["id"]]
        received[row["id"]][row["arm"]] = row
    assert set(received) == {c["id"] for c in cases}
    pairs = [measure(c, received[c["id"]]) for c in cases]
    output = dict(
        all=aggregate(pairs), eligible=aggregate([p for p in pairs if p["eligible"]]), pairs=pairs
    )
    dump(HERE / f"{label}.json", output)
    print(json.dumps(output["all"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("label", choices=["primary", "pressure"])
    args = parser.parse_args()
    main(args.path, args.label)
