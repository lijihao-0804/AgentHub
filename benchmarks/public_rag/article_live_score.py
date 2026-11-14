"""Use the published first-response captures to summarize blinded assessment and preservation."""

from collections import Counter

from benchmarks.public_rag.article_live_blind import PUBLIC, ROOT, WORK, read, write

ARMS = ("baseline", "article_local_passage", "full_parent4")


def score(raw_dir=PUBLIC, review_dir=WORK, destination=PUBLIC / "review.json"):
    protocol = read(PUBLIC / "protocol-public.json")
    cases = {c["id"]: c for c in protocol["cases"]}
    from benchmarks.public_rag.article_live_replay import project

    train = ROOT / "data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json"
    evidence = {c["id"]: c["evidence"] for c in project(train)[1]}
    decisions = {}
    for scope in ("full", "qasper"):
        decisions.update(read(review_dir / f"{scope}-judgments.json"))
    details = {}
    for scope in ("full", "qasper"):
        key = read(review_dir / f"{scope}-key.json")
        for arm in ARMS:
            for row in read(raw_dir / f"{scope}-{arm}.json")["rows"]:
                cid = row["id"]
                case = cases[cid]
                verdict, reason = decisions[key[arm][cid]]
                assert verdict in {
                    "supported",
                    "safe_refusal",
                    "abstained",
                    "unsupported",
                    "http_failure",
                    "incorrect",
                }
                if row["http"] != 200:
                    assert verdict == "http_failure"
                if verdict == "supported":
                    assert row["citations"] and not row["insufficient"]
                meta = details.setdefault(
                    cid, {"scope": scope, "query": case["query"], "type": case["type"]}
                )
                gold_paragraph = case.get("evidence_sha256", [])
                visible = None
                if scope == "qasper" and gold_paragraph:
                    visible = any(
                        ev in h["excerpt"]
                        for ev in evidence[cid]
                        for h in row.get("citations", [])
                        if h["source"] in case["gold_documents"]
                    )
                meta[arm] = {
                    "verdict": verdict,
                    "reason": reason,
                    "http": row["http"],
                    "cited_gold": any(
                        c["source"] in case["gold_documents"] for c in row.get("citations", [])
                    ),
                    "audit_key": key[arm][cid],
                    "any_annotated_paragraph_contained_in_one_returned_excerpt": visible,
                }
    summary = {}

    def metric(arm, pop):
        return dict(Counter(d[arm]["verdict"] for d in pop))

    for subset in ("all", "full", "qasper"):
        pop = {i: d for i, d in details.items() if subset == "all" or d["scope"] == subset}
        control = [i for i, d in pop.items() if d["baseline"]["verdict"] == "supported"]
        summary[subset] = {"count": len(pop), "baseline_supported_ids": control, "arms": {}}
        for arm in ARMS:
            summary[subset]["arms"][arm] = {
                "outcomes": metric(arm, pop.values()),
                "preserved": [i for i in control if pop[i][arm]["verdict"] == "supported"],
                "lost": [i for i in control if pop[i][arm]["verdict"] != "supported"],
                "gained": [
                    i
                    for i, d in pop.items()
                    if i not in control and d[arm]["verdict"] == "supported"
                ],
            }
    historical = ("791", "838-reproductive-functions")
    summary["historically_supported"] = {
        arm: {qid: details[qid][arm]["verdict"] for qid in historical} for arm in ARMS
    }
    negative_ids = [
        c["id"] for c in cases.values() if c.get("negative") or c["id"] == "missing-tenant-canary"
    ]
    summary["annotated_or_synthetic_negative"] = {
        arm: {qid: details[qid][arm]["verdict"] for qid in negative_ids} for arm in ARMS
    }
    write(
        destination,
        {
            "method": (
                "Single assistant review of arm-blinded answers and actual output citations. "
                "Exact repeats share one judgment. Provider errors counted."
            ),
            "results": details,
            "summary": summary,
        },
    )


if __name__ == "__main__":
    score()
