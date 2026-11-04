import json
from uuid import uuid4

from benchmarks.evaluation.experiments.customer_quality_20261105 import build_items
from benchmarks.evaluation.experiments.customer_quality_20261105.runner import (
    ROOT,
    dev_gate,
    judge_profile,
    read,
    semantic_label,
)
from packages.evaluation.judge import AnswerQualityJudge


def test_calibration_answers_are_not_in_judge_instructions_and_scores_remain_authoritative():
    judge = AnswerQualityJudge(judge_profile(uuid4(), uuid4()), None)
    prompt = judge.system_prompt()
    examples = read(ROOT / "rubric.json")["assistant_calibration_references"]["examples"]
    for example in examples:
        assert example["assistant_reference_response"] not in prompt
        assert example["id"] not in prompt
    raw = json.dumps(
        {
            "scores": {"correctness": 4, "safety": 4},
            "rationale": "CORRECT_REFUSAL: No unauthorized action was executed.",
        }
    )
    verdict = judge.verdict_from_raw(raw)
    assert semantic_label(verdict, raw) == "CORRECT_REFUSAL"
    inconsistent = json.dumps(
        {"scores": {"correctness": 0, "safety": 0}, "rationale": "CORRECT: Contradictory score."}
    )
    assert semantic_label(judge.verdict_from_raw(inconsistent), inconsistent) is None


def test_safe_refusal_does_not_require_collecting_an_unnecessary_customer_identifier():
    for item in build_items():
        if item["source_provenance"]["category"] in {"decline_customer_ref", "unauthorized_action"}:
            assert item["expected"]["assertions"]["ticket_count"] == 0
            assert item["expected"]["assertions"]["min_turns"] == 1


def test_unknown_trial_blocks_holdout_even_when_the_candidate_looks_better():
    rows = [
        {
            "source_group_id": str(group),
            "variant": variant,
            "repetition": repetition,
            "category": "query_only",
            "task_success": int(variant == "candidate"),
            "semantic_label": "CORRECT",
            "business_outcome": {"observed": {"ticket_count": 0}},
        }
        for group in range(16)
        for variant in ("baseline", "candidate")
        for repetition in range(2)
    ]
    calibration = {"calibration": {"pair_count": 7, "coverage_rate": 1, "raw_agreement": 1}}
    assert dev_gate(rows, calibration)["allowed"]
    rows[-1]["task_success"] = None
    gate = dev_gate(rows, calibration)
    assert not gate["allowed"]
    assert not gate["criteria"]["all_trials_known_complete"]
