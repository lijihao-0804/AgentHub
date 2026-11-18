"""Verify the numbered first message and report paired, question-level evidence delivery."""

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from .freeze import HERE, OUT, ROOT, as_bytes, file_hash, registered_case, sha

HEADER = re.compile(
    r"(?m)^\[(\d+)\]\nsource: ([^\n]+)\nlocator: ([^\n]+)\n"
    r"(?:document_lifecycle: [^\n]+\n)?text:\n"
)


def checked(condition, message):
    if not condition:
        raise ValueError(message)


def rows(path):
    with path.open("rb") as stream:
        for line in stream:
            yield json.loads(line)


def collect(path):
    table = defaultdict(dict)
    for row in rows(path):
        arms = table[row["id"]]
        checked(row["arm"] in ("baseline", "candidate"), "Unknown arm")
        checked(row["arm"] not in arms, "Duplicate question/arm receipt")
        arms[row["arm"]] = row
    return table


def sources_for(case):
    result = {}
    for p in case["paragraphs"]:
        data = (ROOT / p["path"]).read_bytes().decode()
        checked(data == p["paragraph_text"].strip(), "Paragraph file differs from frozen text")
        result[p["source_id"]] = data
    checked(len(result) == 20, "Expected 20 question-local sources")
    for g in case["gold_spans"]:
        text = result[g["source_id"]]
        checked(
            (g["start"], g["end"], g["sha256"]) == (0, len(text), sha(text.encode())),
            "Official supporting span differs from registered source",
        )
    return result


def interval(record, sources):
    locator = record["locator"]
    left, right = locator["char_start"], locator["char_end"]
    text = sources[record["source_id"]]
    checked(0 <= left < right <= len(text), "Locator outside original source")
    raw = text[left:right]
    checked(raw.strip() == record["text"], "Located text differs from actual passage")
    return left + len(raw) - len(raw.lstrip()), right - len(raw) + len(raw.rstrip())


def complete(text, ranges):
    position = 0
    for left, right in sorted(ranges):
        if left > position and text[position:left].strip():
            return False
        position = max(position, right)
    return not text[position:].strip()


def coverage(records, sources, gold):
    hits = defaultdict(list)
    for record in records:
        hits[record["source_id"]].append(interval(record, sources))
    return dict(
        visible=sorted(gold.intersection(hits)),
        full=sorted(s for s in gold if complete(sources[s], hits.get(s, ()))),
    )


def minimum_pool_chunks(ranked, sources, gold):
    """Minimum number of individual chunks from this pool needed to cover all gold."""
    by_source = defaultdict(list)
    for r in ranked:
        if r["source_id"] in gold:
            by_source[r["source_id"]].append(interval(r, sources))
    count = 0
    for source_id in gold:
        spans = by_source[source_id]
        text = sources[source_id]
        if not complete(text, spans):
            return None
        count += next(
            k
            for k in range(1, len(spans) + 1)
            if any(complete(text, choice) for choice in combinations(spans, k))
        )
    return count


def first_prompt(row, case):
    messages = row["events"][0]["messages"]
    users = [m["content"] for m in messages if m["role"] == "user"]
    checked(len(users) == 1, "Expected exactly one user message at the first physical attempt")
    prompt = users[0]
    checked(prompt.startswith("Question:\n" + case["question"] + "\n"), "Wrong presented question")
    headers = list(HEADER.finditer(prompt))
    selected = row["selected"]
    checked(0 < len(headers) == len(selected) <= 6, "Numbered evidence count differs")
    for i, (h, record) in enumerate(zip(headers, selected, strict=True)):
        checked(int(h[1]) == record["id"] == i + 1, "Presented citation number differs")
        checked(h[2].endswith("/revision:" + record["revision"]), "Wire source revision differs")
        if "source" in record:
            checked(h[2] == record["source"], "Wire source reference differs")
        checked(json.loads(h[3]) == record["locator"], "Presented locator differs")
        end = headers[i + 1].start() - 2 if i + 1 < len(headers) else len(prompt)
        checked(prompt[h.end() : end] == record["text"], "Actual numbered passage text differs")
    # Use the same canonical message serialization as the transport registration.
    return sha(
        json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def ranks(records):
    return [r["chunk_id"] for r in records]


def stage_ids(arm, stage):
    return ranks(arm["body"]["retrieval_trace"]["stages"][stage]["results"])


def checked_pool(arms, historical):
    checked(set(arms) == {"baseline", "candidate"}, "An arm of the pair is missing")
    base, cand = arms["baseline"], arms["candidate"]
    ranked = cand["ranked"]
    checked(6 <= len(ranked) <= 20, "Unregistered rerank pool size")
    for stage in ("dense", "sparse", "fused", "selection"):
        checked(stage_ids(base, stage) == stage_ids(cand, stage), f"Arm difference in {stage}")
    for row in arms.values():
        # The public trace can be truncated at final_top_k. The observer also captures the
        # full ranked list immediately before overlap suppression in the new experiment.
        trace = stage_ids(row, "rerank")
        checked(6 <= len(trace) <= len(ranked), "Unexpected rerank trace length")
        checked(trace == ranks(ranked)[: len(trace)], "Captured ranking differs from trace")
    checked(set(stage_ids(cand, "fused")) == set(ranks(ranked)), "Rerank and fused pools differ")
    checked(len(set(ranks(ranked))) == len(ranked), "Duplicate ranked chunk")
    if historical:
        checked(ranks(base["ranked"]) == ranks(ranked[:6]), "Historical first six differ")
    else:
        checked(base["ranked"] == ranked, "Ranked candidates differ across arms")
        for field in ("rerank_input", "deduped"):
            checked(base[field] == cand[field], f"Paired {field} differs")
    return ranked


def verify_identity(selected, ranked):
    by_chunk = {r["chunk_id"]: r for r in ranked}
    for record in selected:
        locator = record["locator"]
        parent = by_chunk[record["chunk_id"]]
        components = locator.get("components") if locator.get("type") == "source_union" else None
        if components:
            intervals = []
            for component in components:
                raw = by_chunk[component["chunk_id"]]
                checked(
                    component["locator"] == raw["locator"]
                    and raw["revision"] == record["revision"]
                    and raw["source_id"] == record["source_id"],
                    "Composite component identity differs",
                )
                intervals.append((raw["locator"]["char_start"], raw["locator"]["char_end"]))
            intervals.sort()
            start, end = intervals[0]
            for left, right in intervals[1:]:
                checked(left <= end, "Unattributed gap in composite passage")
                end = max(end, right)
            checked(
                (start, end) == (locator["char_start"], locator["char_end"]),
                "Composite boundaries differ",
            )
        else:
            checked(locator == parent["locator"], "Selected locator differs from ranked locator")
        checked(
            parent["source_id"] == record["source_id"] and parent["revision"] == record["revision"],
            "Selected source differs from ranked source",
        )


def measure(case, arms, *, historical=False):
    sources = sources_for(case)
    gold = {g["source_id"] for g in case["gold_spans"]}
    pool = checked_pool(arms, historical)
    source_revisions = {(r["source_id"], r["revision"]) for r in pool}
    checked(
        len({s for s, _ in source_revisions})
        == len(source_revisions)
        == len({v for _, v in source_revisions}),
        "Ambiguous source-to-revision binding",
    )
    result = dict(
        id=case["id"],
        stratum=case.get("stratum", "historical"),
        pool=coverage(pool, sources, gold),
        pool_count=len(pool),
        min_raw_chunks_for_all_gold=minimum_pool_chunks(pool, sources, gold),
        ranked_sha256=sha(as_bytes(ranks(pool))),
    )
    if not historical:
        by_id = {r["chunk_id"]: r for r in pool}
        deduped = [by_id[chunk_id] for chunk_id in arms["baseline"]["deduped"]]
        result["after_overlap"] = coverage(deduped, sources, gold)
        result["min_retained_chunks_for_all_gold"] = minimum_pool_chunks(deduped, sources, gold)
    fullpool = {r["chunk_id"]: i for i, r in enumerate(pool, 1)}
    for arm, row in arms.items():
        selected = row["selected"]
        verify_identity(selected, pool)
        result[arm] = dict(
            **coverage(selected, sources, gold),
            message_sha256=first_prompt(row, case),
            selected=[
                {
                    "slot": s["id"],
                    "idx": int(s["source_id"].rsplit("::", 1)[1]),
                    "rank": fullpool[s["chunk_id"]],
                    "chars": [s["locator"]["char_start"], s["locator"]["char_end"]],
                    "chunk_id": s["chunk_id"],
                }
                for s in selected
            ],
        )
    return result


def paired_counts(rows_, predicate):
    counts = Counter((predicate(row["baseline"]), predicate(row["candidate"])) for row in rows_)
    gain, loss = counts[False, True], counts[True, False]
    discordant = gain + loss
    exact_p = min(
        1.0, 2 * sum(math.comb(discordant, j) for j in range(min(gain, loss) + 1)) / 2**discordant
    )
    return dict(
        pairs=len(rows_),
        baseline=counts[True, True] + loss,
        candidate=counts[True, True] + gain,
        gains=gain,
        losses=loss,
        tied_positive=counts[True, True],
        tied_negative=counts[False, False],
        net=gain - loss,
        mcnemar_two_sided_exact=exact_p,
    )


def summarize(results):
    report = {}
    for key_ in ("visible", "full"):
        report[key_ + "_both"] = paired_counts(results, lambda a, key_=key_: len(a[key_]) == 2)
        for arm in ("pool", "after_overlap", "baseline", "candidate"):
            report.setdefault("individual_" + key_, {})[arm] = sum(
                len(r[arm][key_]) for r in results
            )
        report.setdefault("candidate_pool_both", {})[key_] = sum(
            len(r["pool"][key_]) == 2 for r in results
        )
        for arm, other, label in (
            ("candidate", "baseline", "gained"),
            ("baseline", "candidate", "lost"),
        ):
            report["individual_" + key_][label] = sum(
                len(set(r[arm][key_]) - set(r[other][key_])) for r in results
            )
    for stage in ("raw", "retained"):
        metric = f"min_{stage}_chunks_for_all_gold"
        report[f"min_{stage}_chunks_for_both_distribution"] = dict(
            Counter("unavailable" if r[metric] is None else str(r[metric]) for r in results)
        )
    report["different_wire_inputs"] = sum(
        r["baseline"]["message_sha256"] != r["candidate"]["message_sha256"] for r in results
    )
    return report


def run(path, output):
    registered = json.loads((HERE / "manifest.json").read_bytes())
    cases = json.loads((OUT / "matched.json").read_bytes())["cases"]
    for c, r in zip(cases, registered["cases"], strict=True):
        identity = registered_case(
            c, c["stratum"], r["lexical_control"], r["file"], r["line"], r["row_sha256"]
        )
        checked(
            identity == r and c["gold_spans"] == r["gold_spans"] and c["cohort"] == r["cohort"],
            "Matched data or gold differs from frozen registration",
        )
    data = collect(path)
    cases = [c for c in cases if c["cohort"] != "smoke"]
    checked(set(data) == {c["id"] for c in cases}, "Complete 64 paired formal questions required")
    details = [measure(c, data[c["id"]]) for c in cases]
    primary = [r for r in details if r["stratum"].startswith("primary_")]
    out = dict(
        schema=1,
        receipt_sha256=file_hash(path),
        manifest_sha256=file_hash(HERE / "manifest.json"),
        all_primary=summarize(primary),
        by_stratum={
            k: summarize([r for r in primary if r["stratum"] == k])
            for k in ("primary_long", "primary_ordinary")
        },
        candidate_pool_complete=summarize([r for r in primary if len(r["pool"]["full"]) == 2]),
        insufficient_pairs=len(details) - len(primary),
        evidence_pairs=details,
    )
    output.write_bytes(as_bytes(out))
    print(json.dumps(out["all_primary"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.receipts, args.output)
