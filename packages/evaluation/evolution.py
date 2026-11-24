"""Four-layer trajectory failure attribution and minimal-diff evolution gating.

Implements Chapter 9's continuous-evolution loop on top of AgentHub's frozen
evaluation primitives:
1. Classify each failed trajectory into the right update layer:
   - KNOWLEDGE_BASE: missing/stale facts or retrieval gap.
   - PROMPT_OR_SKILL: evidence was present, but instruction/boundary failed.
   - HARNESS_OR_TOOL: deterministic tool schema, retry, permission, or budget error.
   - MODEL_CAPABILITY: repeated reasoning/format error despite complete context.
2. Cluster failures while carrying preserved passing cases (regression guards)
   and filtering out previously rejected proposal signatures (Self-Harness).
3. Dry-run gate checks both target failure recovery AND zero regression on
   preserved passing cases before promoting to a formal paired Experiment.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from packages.core.canonical.json_hash import canonical_json_hash


class EvolutionTargetLayer(StrEnum):
    KNOWLEDGE_BASE = "KNOWLEDGE_BASE"
    PROMPT_OR_SKILL = "PROMPT_OR_SKILL"
    HARNESS_OR_TOOL = "HARNESS_OR_TOOL"
    MODEL_CAPABILITY = "MODEL_CAPABILITY"


@dataclass(frozen=True, slots=True)
class TrajectoryOutcome:
    case_id: str
    passed: bool
    failure_category: str | None = None
    evidence_sufficient: bool | None = None
    tool_error_code: str | None = None
    feedback_note: str | None = None


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    signature: str
    target_layer: EvolutionTargetLayer
    cluster_key: str
    failure_count: int
    supporting_failure_ids: tuple[str, ...]
    regression_guard_ids: tuple[str, ...]
    minimal_diff_hint: str


@dataclass(frozen=True, slots=True)
class DryRunVerdict:
    verdict: str
    fixed_count: int
    guard_regressions: tuple[str, ...]
    still_failing_ids: tuple[str, ...]


_HARNESS_CATEGORIES = frozenset(
    {
        "TOOL_ARGUMENT_INVALID",
        "TOOL_EXECUTION_FAILED",
        "TOOL_TIMEOUT",
        "MAX_STEPS_EXCEEDED",
        "MAX_TOOL_CALLS_EXCEEDED",
        "CONTEXT_BUDGET_EXCEEDED",
        "APPROVAL_REQUIRED",
    }
)
_KNOWLEDGE_CATEGORIES = frozenset(
    {
        "RETRIEVAL_MISS",
        "EVIDENCE_MISSING",
        "STALE_KNOWLEDGE",
        "CITATION_UNGROUNDED",
    }
)
_PROMPT_CATEGORIES = frozenset(
    {
        "PREMATURE_HANDOFF",
        "WRONG_TOOL_SELECTION",
        "FORMAT_VIOLATION",
        "REFUSAL_MISMATCH",
        "POLICY_MISAPPLICATION",
    }
)


def attribute_failure_layer(outcome: TrajectoryOutcome) -> EvolutionTargetLayer:
    """Map one failed trajectory to the most appropriate evolution layer."""
    cat = (outcome.failure_category or "").upper()
    tool_err = (outcome.tool_error_code or "").upper()

    if tool_err or cat in _HARNESS_CATEGORIES:
        return EvolutionTargetLayer.HARNESS_OR_TOOL
    if outcome.evidence_sufficient is False or cat in _KNOWLEDGE_CATEGORIES:
        return EvolutionTargetLayer.KNOWLEDGE_BASE
    if cat in _PROMPT_CATEGORIES or outcome.evidence_sufficient is True:
        return EvolutionTargetLayer.PROMPT_OR_SKILL
    return EvolutionTargetLayer.MODEL_CAPABILITY


def _hint_for_layer(layer: EvolutionTargetLayer, cluster_key: str) -> str:
    if layer is EvolutionTargetLayer.KNOWLEDGE_BASE:
        return (
            f"Add or update verified source entries / Wiki claims covering '{cluster_key}' "
            "with exact source citations; avoid modifying global system instructions."
        )
    if layer is EvolutionTargetLayer.PROMPT_OR_SKILL:
        return (
            f"Propose a minimal, localized Skill/Prompt diff clarifying boundary conditions "
            f"for '{cluster_key}' with 1-2 concrete before/after examples."
        )
    if layer is EvolutionTargetLayer.HARNESS_OR_TOOL:
        return (
            f"Enforce deterministic code-level guard, schema hint, or retry policy in the "
            f"Harness for '{cluster_key}' rather than relying on prompt reminders."
        )
    return (
        f"Collect diverse verified demonstrations for '{cluster_key}' and evaluate "
        "stronger model routing or fine-tuning."
    )


def build_evolution_proposals(
    outcomes: Sequence[TrajectoryOutcome],
    *,
    rejected_signatures: frozenset[str] = frozenset(),
    min_cluster_size: int = 1,
    max_regression_guards: int = 20,
) -> tuple[EvolutionProposal, ...]:
    """Cluster failed trajectories into minimal-diff proposals with regression guards."""
    guard_ids = tuple(
        sorted(o.case_id for o in outcomes if o.passed)[: max(0, max_regression_guards)]
    )
    clusters: dict[tuple[EvolutionTargetLayer, str], list[str]] = defaultdict(list)

    for outcome in outcomes:
        if outcome.passed:
            continue
        layer = attribute_failure_layer(outcome)
        cluster_key = (
            outcome.failure_category
            or outcome.tool_error_code
            or "UNCLASSIFIED_FAILURE"
        ).upper()
        clusters[(layer, cluster_key)].append(outcome.case_id)

    proposals: list[EvolutionProposal] = []
    for (layer, cluster_key), case_ids in clusters.items():
        if len(case_ids) < min_cluster_size:
            continue
        sorted_ids = tuple(sorted(case_ids))
        signature = canonical_json_hash(
            {"layer": layer.value, "cluster_key": cluster_key, "cases": list(sorted_ids)}
        )
        if signature in rejected_signatures:
            continue
        proposals.append(
            EvolutionProposal(
                signature=signature,
                target_layer=layer,
                cluster_key=cluster_key,
                failure_count=len(sorted_ids),
                supporting_failure_ids=sorted_ids,
                regression_guard_ids=guard_ids,
                minimal_diff_hint=_hint_for_layer(layer, cluster_key),
            )
        )

    proposals.sort(key=lambda p: (-p.failure_count, p.target_layer.value, p.cluster_key))
    return tuple(proposals)


def evaluate_dry_run_gate(
    proposal: EvolutionProposal,
    *,
    after_outcomes: Mapping[str, bool],
) -> DryRunVerdict:
    """Verify a candidate patch fixes target failures with zero guard regressions."""
    regressions = tuple(
        cid for cid in proposal.regression_guard_ids if after_outcomes.get(cid) is not True
    )
    still_failing = tuple(
        cid for cid in proposal.supporting_failure_ids if after_outcomes.get(cid) is not True
    )
    fixed_count = len(proposal.supporting_failure_ids) - len(still_failing)

    if regressions:
        verdict = "REJECT_REGRESSION"
    elif fixed_count <= 0:
        verdict = "REJECT_NO_GAIN"
    else:
        verdict = "APPROVE_FOR_EXPERIMENT"
    return DryRunVerdict(
        verdict=verdict,
        fixed_count=fixed_count,
        guard_regressions=regressions,
        still_failing_ids=still_failing,
    )


__all__ = [
    "DryRunVerdict",
    "EvolutionProposal",
    "EvolutionTargetLayer",
    "TrajectoryOutcome",
    "attribute_failure_layer",
    "build_evolution_proposals",
    "evaluate_dry_run_gate",
]
