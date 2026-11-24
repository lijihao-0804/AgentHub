"""Unit tests for four-layer failure attribution and minimal-diff evolution gating."""

from __future__ import annotations

from packages.evaluation.evolution import (
    EvolutionTargetLayer,
    TrajectoryOutcome,
    attribute_failure_layer,
    build_evolution_proposals,
    evaluate_dry_run_gate,
)


def test_attribute_failure_layer_separates_four_update_layers() -> None:
    assert (
        attribute_failure_layer(
            TrajectoryOutcome(case_id="c1", passed=False, tool_error_code="TOOL_ARGUMENT_INVALID")
        )
        is EvolutionTargetLayer.HARNESS_OR_TOOL
    )
    assert (
        attribute_failure_layer(
            TrajectoryOutcome(case_id="c2", passed=False, evidence_sufficient=False)
        )
        is EvolutionTargetLayer.KNOWLEDGE_BASE
    )
    assert (
        attribute_failure_layer(
            TrajectoryOutcome(
                case_id="c3",
                passed=False,
                failure_category="PREMATURE_HANDOFF",
                evidence_sufficient=True,
            )
        )
        is EvolutionTargetLayer.PROMPT_OR_SKILL
    )
    assert (
        attribute_failure_layer(TrajectoryOutcome(case_id="c4", passed=False))
        is EvolutionTargetLayer.MODEL_CAPABILITY
    )


def test_build_proposals_excludes_rejected_and_dry_run_enforces_zero_regression() -> None:
    outcomes = [
        TrajectoryOutcome(case_id="pass-1", passed=True),
        TrajectoryOutcome(case_id="pass-2", passed=True),
        TrajectoryOutcome(
            case_id="fail-1",
            passed=False,
            failure_category="PREMATURE_HANDOFF",
            evidence_sufficient=True,
        ),
        TrajectoryOutcome(
            case_id="fail-2",
            passed=False,
            failure_category="PREMATURE_HANDOFF",
            evidence_sufficient=True,
        ),
        TrajectoryOutcome(
            case_id="fail-3",
            passed=False,
            failure_category="RETRIEVAL_MISS",
            evidence_sufficient=False,
        ),
    ]
    proposals = build_evolution_proposals(outcomes)
    assert len(proposals) == 2
    top = proposals[0]
    assert top.target_layer is EvolutionTargetLayer.PROMPT_OR_SKILL
    assert top.failure_count == 2
    assert top.regression_guard_ids == ("pass-1", "pass-2")

    # Dry-run approves when target fixed and zero guard regressions
    ok = evaluate_dry_run_gate(
        top,
        after_outcomes={"pass-1": True, "pass-2": True, "fail-1": True, "fail-2": False},
    )
    assert ok.verdict == "APPROVE_FOR_EXPERIMENT"
    assert ok.fixed_count == 1
    assert ok.guard_regressions == ()

    # Dry-run rejects if any preserved passing guard regresses
    regressed = evaluate_dry_run_gate(
        top,
        after_outcomes={"pass-1": True, "pass-2": False, "fail-1": True, "fail-2": True},
    )
    assert regressed.verdict == "REJECT_REGRESSION"
    assert regressed.guard_regressions == ("pass-2",)

    # Self-Harness: previously rejected proposal signature is skipped
    filtered = build_evolution_proposals(outcomes, rejected_signatures=frozenset({top.signature}))
    assert all(p.signature != top.signature for p in filtered)
