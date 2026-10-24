import json
from copy import deepcopy
from pathlib import Path

import pytest

from benchmarks.public_rag.attributed_reader_report import score
from benchmarks.public_rag.attributed_reader_runner import PROFILES
from tests.unit.test_public_rag_consumer_report import _arm


def fixture():
    if not (
        Path(__file__).resolve().parents[2]
        / "benchmarks/public_rag/results/attributed-reader-20261023/protocol.json"
    ).is_file():
        pytest.skip("requires the local-only attributed-reader experiment protocol")
    protocol = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "benchmarks/public_rag/results/attributed-reader-20261023/protocol.json"
        ).read_text(encoding="utf-8")
    )
    rows = []
    for i in range(12):
        arms = {}
        for p in PROFILES:
            arm = _arm(
                0.5 if p == "attribute512_answer256" else 0.4,
                recall=0.8,
                usage={"prompt_tokens": 100, "total_tokens": 110},
            )
            candidate = p == "attribute512_answer256"
            arm.update(
                attribution_status="VALID" if candidate else "NOT_REQUIRED",
                pipeline_ms=20 if candidate else 10,
                source_fragments_unchanged=True,
                calls={"answer": deepcopy(arm["api"])} if p != "direct256_prior" else {},
            )
            if candidate:
                arm["calls"]["attribute"] = deepcopy(arm["api"])
            arms[p] = arm
        rows.append({"query_id": str(i), "group_id": str(i % 6), "hops": 2, "arms": arms})
    meta = {"queries": 12, "profiles": PROFILES, "protocol": protocol}
    done = {
        "queries": 12,
        "expected": 12,
        "completed_profiles": 24,
        "run_state": "COMPLETE",
        "physical_calls": 36,
        "prior_control_reuses": 12,
        "candidate_fallback_reuses": 0,
    }
    return rows, meta, done, protocol


def test_answer_gains_do_not_hide_invalid_spans_or_changed_original_sources():
    args = fixture()
    assert score(*args)["passed"]
    for row in args[0][:2]:
        row["arms"]["attribute512_answer256"]["attribution_status"] = "INVALID_SPANS"
    assert not score(*args)["gates"]["attribution_valid"]
    args = fixture()
    args[0][0]["arms"]["attribute512_answer256"]["source_fragments_unchanged"] = False
    assert not score(*args)["passed"]


def test_historical_control_cannot_replace_the_live_matched_output_control():
    args = fixture()
    for row in args[0]:
        row["arms"]["direct768"]["token_f1"] = 0.6
    report = score(*args)
    assert report["primary_f1"]["delta"] < 0
    assert not report["passed"]


def test_missing_stage_usage_cannot_be_counted_as_zero_overhead():
    args = fixture()
    for row in args[0][:2]:
        row["arms"]["attribute512_answer256"]["calls"]["attribute"]["usage"] = None
    report = score(*args)
    assert not report["gates"]["paired_usage_coverage"]
    assert report["physical_api"]["usage_unknown"] == 2
