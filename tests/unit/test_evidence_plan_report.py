import json
from copy import deepcopy
from pathlib import Path

import pytest

from benchmarks.public_rag.evidence_plan_report import score
from tests.unit.test_public_rag_consumer_report import _arm


def _fixture():
    path = Path(__file__).resolve().parents[2] / (
        "benchmarks/public_rag/results/evidence-plan-20261023/protocol.json"
    )
    if not path.is_file():
        pytest.skip("requires the local-only evidence-plan experiment protocol")
    protocol = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "benchmarks/public_rag/results/evidence-plan-20261023/protocol.json"
        ).read_text(encoding="utf-8")
    )
    profiles = ["legacy5", "compact5", "compact5_matched"]
    rows = []
    for i in range(100):
        arms = {
            p: _arm(0.5, recall=0.8, usage={"prompt_tokens": 100 if p == "legacy5" else 70})
            for p in profiles
        }
        rows.append({"query_id": f"q{i}", "group_id": f"g{i % 20}", "hops": 2, "arms": arms})
    meta = {"profiles": profiles, "protocol": protocol, "queries": 100}
    done = {"queries": 100, "expected": 100, "run_state": "COMPLETE", "observed_calls": 300}
    return rows, meta, done, protocol


def test_token_savings_may_pass_only_the_predeclared_noninferiority_path():
    rows, meta, done, protocol = _fixture()
    report = score(rows, meta, done, protocol)
    assert report["efficiency_passed"] and not report["quality_passed"]
    assert report["primary_f1_comparison"]["delta"] == 0


def test_missing_usage_or_broken_baseline_cannot_manufacture_efficiency_acceptance():
    rows, meta, done, protocol = _fixture()
    unknown = deepcopy(rows)
    for row in unknown[:6]:
        row["arms"]["compact5"]["api"]["usage"] = None
    assert not score(unknown, meta, done, protocol)["passed"]
    for row in rows[:6]:
        row["arms"]["legacy5"] = _arm(0, recall=0.8, status="INVALID_OUTPUT", usage=None)
    assert not score(rows, meta, done, protocol)["passed"]
