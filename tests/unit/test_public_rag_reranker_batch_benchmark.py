import json

import pytest

from benchmarks.public_rag.reranker_batch_benchmark import PROTOCOL, Case, summarize
from packages.knowledge.contracts import RerankCandidate


def test_replay_preserves_ties_and_rejects_partial_or_nonfinite_scores():
    case = Case(
        "query",
        tuple(RerankCandidate(k, k) for k in ("c", "b", "a")),
        ("source-c", "source-b", "source-a"),
        (0.1, 0.2, 0.2),
    )
    assert case.ranked_indices([1, 1, 1]) == [2, 1, 0]
    for bad in ([1], [1, 1, float("nan")]):
        with pytest.raises(ValueError, match="adapter scores"):
            case.ranked_indices(bad)


def test_consumer_cut_precedes_source_dedup_and_input_drift_fails_the_gate():
    protocol = json.loads(PROTOCOL.read_bytes())
    protocol["gates"]["required_complete_pairs"] = 2
    protocol["sample"]["candidates_per_query"] = 6
    protocol["bootstrap"]["replicates"] = 32
    candidates = tuple(RerankCandidate(str(i), str(i)) for i in range(6))
    case = Case("query", candidates, ("noise",) * 5 + ("gold",), (0,) * 6)
    a = {"scores": [6, 5, 4, 3, 2, 1], "ms": 2}
    b = {"scores": a["scores"], "ms": 1}
    rows = [
        {"id": q, "first": arm, "baseline": a, "candidate": b}
        for q, arm in [("q1", "baseline"), ("q2", "candidate")]
    ]
    shapes = [{"pairs": 6, "width": 8, "real": 42}]
    audits = {
        r["id"]: {"baseline": shapes, "candidate": shapes, "identical_inputs": True} for r in rows
    }
    cases = {r["id"]: case for r in rows}
    qrels = {r["id"]: {"gold": 1} for r in rows}
    summary = summarize(protocol, cases, qrels, rows, audits)
    assert summary["decision"] == "PASS"
    assert summary["bootstrap_groups"] == 1
    assert summary["quality"]["raw5_source_recall"]["baseline"] == 0
    assert summary["quality"]["pool_source_recall"]["baseline"] == 1
    assert summary["quality"]["document_ndcg10"]["baseline"] == pytest.approx(1 / 1.5849625)
    assert summary["adapter_latency_ms"]["ci95"] == [-1, -1]
    audits["q2"]["identical_inputs"] = False
    assert summarize(protocol, cases, qrels, rows, audits)["decision"] == "FAIL"
