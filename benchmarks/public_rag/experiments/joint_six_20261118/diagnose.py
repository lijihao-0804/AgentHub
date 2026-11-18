"""Reconstruct the six historical packing losses and enumerate minimum fixed-slot repairs."""

import json
from itertools import combinations

from .freeze import HERE, ROOT, archive, as_bytes, file_hash
from .score import checked, collect, coverage, measure, sources_for

FRESH = ROOT / ".scratch/composite-qa-preflight/fresh.jsonl"
OLD_CASES = ROOT / ".scratch/merge-refill-fresh/matched.json"

# Literal boundaries used only in the historical qualitative audit. These do not train or
# enter the new selector. The two slices per case ground the described bridge and terminal fact.
FACTS = {
    "2hop__75213_24779": [
        (9, "In 1858 Charles Darwin", "general concept of evolution"),
        (2, "Darwin made extensive revisions", 'used the word "evolution"'),
    ],
    "2hop__37658_68056": [
        (0, "Lutheranism spread from Germany", "its surrounding areas"),
        (7, "Military Administration in France", "1940 -- 1944"),
    ],
    "2hop__767960_92444": [
        (5, '"Mistletoe" is a Christmas song', "Canadian singer Justin Bieber"),
        (3, "After the second verse, Ludacris", "the verse - rap"),
    ],
    "2hop__136704_91206": [
        (16, '"Portrait in Black" was filmed', "San Francisco"),
        (4, "The Transamerica Pyramid", "Financial District of San Francisco"),
    ],
    "2hop__161005_489272": [
        (15, "Depp compared them to modern rock stars", "base his performance on Keith Richards"),
        (14, "the daughter of Patti Hansen", "guitarist Keith Richards"),
    ],
    "2hop__435776_81382": [
        (2, "Arecibo () is a municipality", "north of Utuado and Ciales"),
        (0, "Puerto Rico formed", "a key part of the Spanish Empire"),
    ],
}


def substitutions(presented, ranked, gold, sources):
    existing = {
        component["chunk_id"]
        for s in presented
        for component in s["locator"].get("components", [s])
    }
    eligible = [
        (i, s)
        for i, s in enumerate(ranked, 1)
        if s["chunk_id"] not in existing and s["source_id"] in gold
    ]
    for count in range(1, min(len(presented), len(eligible)) + 1):
        repairs = []
        for removed in combinations(range(len(presented)), count):
            retained = [s for i, s in enumerate(presented) if i not in removed]
            for added in combinations(eligible, count):
                trial = retained + [s for _, s in added]
                if set(coverage(trial, sources, gold)["full"]) == gold:
                    repairs.append(
                        dict(drop_slots=[i + 1 for i in removed], add_ranks=[i for i, _ in added])
                    )
        if repairs:
            return dict(minimum=count, options=repairs)
    raise ValueError("A covered pool should admit an atomic repair for these historical cases")


def describe(r, ranked, case, trace):
    by_id = {p["chunk_id"]: i for i, p in enumerate(ranked, 1)}
    pars = {p["source_id"]: p for p in case["paragraphs"]}
    p = pars[r["source_id"]]
    components = r["locator"].get("components", [r])
    return dict(
        idx=p["idx"],
        title=p["title"],
        gold=p["is_supporting"],
        chars=[r["locator"]["char_start"], r["locator"]["char_end"]],
        original_ranks=[by_id[c["chunk_id"]] for c in components],
        fused_ranks=[trace[c["chunk_id"]]["rank"] for c in components],
        chunk_ids=[c["chunk_id"] for c in components],
    )


def fact_spans(case, selected, sources):
    spans = []
    for idx, start_token, end_token in FACTS[case["id"]]:
        s = f"{case['id']}::paragraph::{idx}"
        body = sources[s]
        start = body.index(start_token)
        end = body.index(end_token, start) + len(end_token)
        slots = [
            r["id"]
            for r in selected
            if r["source_id"] == s
            and r["locator"]["char_start"] <= start
            and end <= r["locator"]["char_end"]
        ]
        spans.append(dict(idx=idx, start=start, end=end, containing_slots=slots))
    return spans


def main():
    checked(
        file_hash(FRESH) == "9faa9016122c54488b21816c0de3ed93b014dcce87f0d39b187cd64dc168b113",
        "Historical preflight differs from registration",
    )
    checked(
        file_hash(OLD_CASES) == "52e871adf5176d1f93f7c5162b34661da0b4be606b93155543215c83ebc22e55",
        "Old data differs from registration",
    )
    all_pairs = collect(FRESH)
    freeze = FRESH.parent / "freeze.json"
    checked(
        file_hash(freeze) == "2f32241f7fc1173524e5fff9a25e1108b5bb0edc96b8652a70637a8a8efc969e",
        "Previous campaign registration differs",
    )
    frozen_messages = json.loads(freeze.read_bytes())["plan"]
    primary = {p["id"]: p for p in archive("primary.json")["pairs"]}
    report = []
    for case in json.loads(OLD_CASES.read_bytes())["cases"]:
        ident = case["id"]
        arms = all_pairs[ident]
        m = measure(case, arms, historical=True)
        archived = primary[ident]
        for arm in ("baseline", "candidate"):
            checked(archived[arm]["full"] == m[arm]["full"], "Historical full metrics differ")
            checked(archived[arm]["visible"] == m[arm]["visible"], "Historical sources differ")
            checked(
                m[arm]["message_sha256"] == frozen_messages[f"{ident}/{arm}"]["message_sha"],
                "First message differs from historical transport registration",
            )
        if len(m["pool"]["full"]) != 2 or len(m["candidate"]["full"]) == 2:
            continue
        sources = sources_for(case)
        gold = set(m["pool"]["full"])
        cand = arms["candidate"]
        ranked = cand["ranked"]
        trace = cand["body"]["retrieval_trace"]["stages"]
        fused = {r["chunk_id"]: r for r in trace["fused"]["results"]}
        rerank = {r["chunk_id"]: r for r in trace["rerank"]["results"]}
        details = dict(
            id=ident,
            gold_indices_in_hop_order=[
                s["paragraph_support_idx"] for s in case["question_decomposition"]
            ],
            min_pool_chunks_to_show_both=m["min_raw_chunks_for_all_gold"],
            all_twenty=[
                dict(
                    **describe(r, ranked, case, fused),
                    rerank_score=rerank[r["chunk_id"]]["score"],
                )
                for r in ranked
            ],
        )
        for arm, observed in arms.items():
            presented = observed["selected"]
            details[arm] = dict(
                selected=[
                    dict(slot=r["id"], **describe(r, ranked, case, fused)) for r in presented
                ],
                present_fact_slices=fact_spans(case, presented, sources),
                minimal_atomic_repairs=substitutions(presented, ranked, gold, sources),
                wire_messages_sha256=m[arm]["message_sha256"],
            )
        report.append(details)
    checked({r["id"] for r in report} == FACTS.keys(), "Six-diagnosis denominator changed")
    out = dict(
        historical_receipt_sha256=file_hash(FRESH),
        historical_matched_sha256=file_hash(OLD_CASES),
        registered_queries=len(primary),
        eligible_pool_and_incomplete_message=len(report),
        ranks="one-based post-cross-encoder ranks; fused ranks are reported separately",
        repair="swap fixed existing slots with unmodified original reranked chunks; no new unions",
        cases=report,
    )
    (HERE / "historical-six.json").write_bytes(as_bytes(out))
    for r in report:
        repairs = r["candidate"]["minimal_atomic_repairs"]
        print(
            r["id"], r["min_pool_chunks_to_show_both"], repairs["minimum"], len(repairs["options"])
        )


if __name__ == "__main__":
    main()
