import json
from copy import deepcopy
from pathlib import Path

import pytest

from benchmarks.public_rag.evidence_set_answers import request_groups
from benchmarks.public_rag.evidence_set_report import score
from benchmarks.public_rag.evidence_set_runner import PROFILES, structure_gate
from tests.unit.test_public_rag_consumer_report import _arm


def fixture():
    root = Path(__file__).resolve().parents[2]
    path = root / "benchmarks/public_rag/results/evidence-set-20261023/protocol-request-repair.json"
    if not path.is_file():
        pytest.skip("requires the local-only evidence-set experiment protocol")
    protocol = json.loads(
        (
            root
            / "benchmarks/public_rag/results/evidence-set-20261023/protocol-request-repair.json"
        ).read_text(encoding="utf-8")
    )
    rows, captures = [], []
    for i in range(20):
        selector = {
            "status": "VALID",
            "pipeline_ms": 5,
            "usage": {"prompt_tokens": 150, "total_tokens": 160},
        }
        row = {
            "query_id": str(i),
            "group_id": str(i),
            "hops": 2,
            "selection": selector,
            "arms": {
                p: _arm(
                    0.4 if p == "legacy5" else 0.5,
                    recall=0.9,
                    usage={"prompt_tokens": 100, "total_tokens": 110},
                )
                for p in PROFILES
            },
        }
        capture = {
            "query_id": str(i),
            "ordering_fragments_equal": True,
            "selection": selector,
            "arms": {p: {"messages": [{"role": "user", "content": f"{i}:{p}"}]} for p in PROFILES},
        }
        rows.append(row)
        captures.append(capture)
    meta = {"protocol": protocol, "profiles": PROFILES, "queries": 20}
    done = {
        "planned_calls": 60,
        "observed_calls": 60,
        "reused_outcomes": 0,
        "logical_outcomes": 60,
        "expected": 20,
        "queries": 20,
        "run_state": "COMPLETE",
    }
    return rows, captures, meta, done, protocol, {"answer_stage_allowed": True}


def test_selector_overhead_and_unknown_usage_cannot_be_hidden_by_answer_gains():
    args = fixture()
    assert score(*args)["passed"]
    rows = deepcopy(args[0])
    rows[0]["selection"] = {**rows[0]["selection"], "usage": None}
    rows[1]["selection"] = {**rows[1]["selection"], "usage": None}
    captures = deepcopy(args[1])
    for i in (0, 1):
        captures[i]["selection"] = rows[i]["selection"]
    report = score(rows, captures, *args[2:])
    assert not report["passed"] and not report["gates"]["usage_coverage"]
    rows, captures, *rest = fixture()
    for row, cap in zip(rows, captures, strict=True):
        row["selection"]["usage"]["prompt_tokens"] = 1000
        cap["selection"] = row["selection"]
    report = score(rows, captures, *rest)
    assert not report["passed"] and not report["gates"]["pipeline_input_budget"]


def test_order_diagnostic_cannot_replace_failing_primary():
    args = fixture()
    for row in args[0]:
        row["arms"]["set_rank5"]["token_f1"] = 0.4
        row["arms"]["set_read5"]["token_f1"] = 0.9
    report = score(*args)
    assert report["full_reading_vs_baseline_diagnostic"]["delta"] > 0.4
    assert not report["passed"] and report["selected_profile"] == "legacy5"


def test_exact_message_reuse_stays_within_question_and_keeps_order_sensitive():
    rows = fixture()[1][:2]
    rows[0]["arms"]["set_read5"]["messages"] = rows[0]["arms"]["set_rank5"]["messages"]
    # Even identical messages across questions are independent planned question outcomes.
    rows[1]["arms"]["set_rank5"]["messages"] = rows[0]["arms"]["set_rank5"]["messages"]
    jobs, reuse = request_groups(rows)
    assert len(jobs) == 5
    assert reuse == {("0", "set_read5"): ("0", "set_rank5")}


def test_frozen_confirmation_scores_only_primary_without_an_ordering_api_arm():
    rows, captures, meta, done, protocol, structure = fixture()
    for row in rows:
        del row["arms"]["set_read5"]
    meta["profiles"] = PROFILES[:2]
    done.update(planned_calls=40, observed_calls=40, logical_outcomes=40)
    report = score(rows, captures, meta, done, protocol, structure)
    assert report["passed"]
    assert report["reading_order_diagnostic"] is None
    assert report["physical_api"]["answers"] == 40


def test_single_support_group_cannot_pass_the_structure_inference_gate():
    protocol = fixture()[4]
    comparison = {
        "complete": {"delta": 0.5, "ci_low": 0.4, "inference_valid": False},
        "recall": {"delta": 0.2, "ci_low": 0.1, "inference_valid": False},
    }
    assert not structure_gate(
        comparison,
        complete=True,
        valid_count=20,
        planned=20,
        ordering_equal=True,
        protocol=protocol,
    )
