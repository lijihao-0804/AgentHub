"""Deterministic, offline analysis for the T04 customer-quality experiment.

The module accepts runner-normalized trial mappings.  It does not execute agents, call a
model, or persist experiment state.  Reliability and uncertainty reuse
``packages.evaluation.statistics`` so this experiment does not create a second set of
statistical definitions.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import mean
from typing import Any

from packages.evaluation.statistics import (
    DEFAULT_BOOTSTRAP_COUNT,
    DEFAULT_SEED,
    TrialOutcome,
    cluster_bootstrap_interval,
    jeffreys_interval,
    summarize_reliability,
)

ANALYSIS_VERSION = "customer-quality-analysis-v1"
ANNOTATION_SOURCE = "assistant_authored_reference"

_UNKNOWN_STATUSES = frozenset({"UNKNOWN", "UNKNOWN_OUTCOME", "NEEDS_ATTENTION"})
_NUMERIC_FIELDS = (
    "semantic_score",
    "cost_estimate_cny",
    "input_tokens",
    "output_tokens",
    "latency_ms",
)
_COMPARISON_FIELDS = (
    "business_pass_hat_k",
    "semantic_score",
    "cost_estimate_cny",
    "input_tokens",
    "output_tokens",
    "latency_ms",
)
_IDENTITY_FIELDS = (
    "variant",
    "model",
    "model_name",
    "model_identity",
    "provider",
    "agent_version",
    "route",
    "deployment",
    "run_id",
)


@dataclass(frozen=True)
class _Trial:
    case_key: str
    source_group_id: str
    split: str
    variant: str
    repetition: int | None
    status: str
    task_success: bool | None
    semantic_score: float | None
    cost_estimate_cny: float | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float | None
    input_hash: str | None
    output_hash: str | None


def analyze_trials(
    rows: Sequence[Mapping[str, object]],
    *,
    k: int,
    baseline_variant: str | None = None,
    candidate_variant: str | None = None,
    bootstrap_count: int = DEFAULT_BOOTSTRAP_COUNT,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Summarize normalized trial rows without treating repetitions as cases.

    ``task_success`` drives business success.  A row without a repetition remains visible
    in raw coverage but cannot enter complete-case ``pass^k`` or a paired comparison.
    When both variant names are supplied, paired deltas are formed at case level and the
    percentile interval resamples one value per ``source_group_id``.
    """

    _validate_k(k)
    _validate_bootstrap(bootstrap_count, seed)
    if (baseline_variant is None) != (candidate_variant is None):
        raise ValueError("baseline_variant and candidate_variant must be supplied together")

    normalized = _normalize_rows(rows, k=k)
    by_variant: dict[str, list[_Trial]] = defaultdict(list)
    for row in normalized:
        by_variant[row.variant].append(row)

    if not by_variant:
        variants: dict[str, dict[str, Any]] = {}
    else:
        variants = {
            variant: _summarize_variant(sorted(variant_rows, key=_trial_sort_key), k=k)
            for variant, variant_rows in sorted(by_variant.items())
        }

    paired = None
    if baseline_variant is not None and candidate_variant is not None:
        if baseline_variant == candidate_variant:
            raise ValueError("baseline_variant and candidate_variant must differ")
        missing = {baseline_variant, candidate_variant} - set(variants)
        if missing:
            raise ValueError(f"comparison variants absent from rows: {sorted(missing)}")
        paired = _compare_variants(
            normalized,
            k=k,
            baseline_variant=baseline_variant,
            candidate_variant=candidate_variant,
            bootstrap_count=bootstrap_count,
            seed=seed,
        )

    return {
        "analysis_version": ANALYSIS_VERSION,
        "k": k,
        "row_count": len(normalized),
        "case_count": len({row.case_key for row in normalized}),
        "variants": variants,
        "paired": paired,
        "statistical_basis": {
            "success_rate": "known task_success trials",
            "pass_hat_k": "complete cases only; repetitions are not independent cases",
            "paired_interval": (
                "percentile bootstrap over case-level deltas grouped by source_group_id"
            ),
            "bootstrap_count": bootstrap_count,
            "seed": seed,
        },
    }


def build_blind_annotation_pack(
    rows: Sequence[Mapping[str, object]],
    *,
    seed: int = DEFAULT_SEED,
    answer_field: str = "answer_text",
) -> dict[str, Any]:
    """Build a deterministic pack that exposes no variant, model, or run identity.

    Anonymous IDs come from a seeded shuffle and a sequence counter.  No content or file
    hash is used.  Only explicit annotation fields and redacted response/input text leave
    this function, so extra runner metadata cannot leak through an allowlist mistake.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if not answer_field or not isinstance(answer_field, str):
        raise ValueError("answer_field must be a non-empty string")

    identity_tokens = {
        value.strip()
        for row in rows
        for field in _IDENTITY_FIELDS
        if isinstance((value := row.get(field)), str) and len(value.strip()) >= 3
    }
    ordered_rows = sorted(rows, key=lambda row: _blind_sort_key(row, answer_field))
    random.Random(seed).shuffle(ordered_rows)

    items: list[dict[str, Any]] = []
    for index, row in enumerate(ordered_rows, start=1):
        answer = row.get(answer_field)
        if not isinstance(answer, str):
            raise ValueError(f"{answer_field} must be text for every pack row")
        input_text = row.get("input_text")
        if input_text is not None and not isinstance(input_text, str):
            raise ValueError("input_text must be text when supplied")
        repetition = row.get("repetition")
        if repetition is not None and (
            isinstance(repetition, bool) or not isinstance(repetition, int) or repetition < 0
        ):
            raise ValueError("repetition must be a non-negative integer or null")
        items.append(
            {
                "anonymous_id": f"ANON-{index:04d}",
                "case_key": _redact_identities(_required_text(row, "case_key"), identity_tokens),
                "source_group_id": _redact_identities(
                    _required_text(row, "source_group_id"), identity_tokens
                ),
                "split": _redact_identities(_required_text(row, "split"), identity_tokens),
                "repetition": repetition,
                "input_text": (
                    _redact_identities(input_text, identity_tokens)
                    if input_text is not None
                    else None
                ),
                "answer_text": _redact_identities(answer, identity_tokens),
                "annotation": None,
            }
        )

    return {
        "pack_version": "customer-quality-blind-pack-v1",
        "annotation_status": "UNLABELED",
        "seed": seed,
        "items": items,
    }


def calibrate_judge(
    reference_labels: Sequence[str | None],
    judge_labels: Sequence[str | None],
    *,
    label_order: Sequence[str],
) -> dict[str, Any]:
    """Compare actual judge labels with the assistant-authored reference labels.

    The returned provenance is intentionally fixed to
    ``assistant_authored_reference``.  Agreement against these references is not a
    human-agreement claim.  Linear weighted kappa is available whenever the paired-label
    distribution yields a non-degenerate expected disagreement.
    """

    if len(reference_labels) != len(judge_labels):
        raise ValueError("reference_labels and judge_labels must have equal length")
    if not label_order or len(set(label_order)) != len(label_order):
        raise ValueError("label_order must contain unique labels")
    allowed = set(label_order)
    for label in (*reference_labels, *judge_labels):
        if label is not None and label not in allowed:
            raise ValueError(f"label not present in label_order: {label!r}")

    pairs = [
        (reference, judge)
        for reference, judge in zip(reference_labels, judge_labels, strict=True)
        if reference is not None and judge is not None
    ]
    confusion = {
        reference: {judge: 0 for judge in label_order} for reference in label_order
    }
    for reference, judge in pairs:
        confusion[reference][judge] += 1

    n = len(pairs)
    agreement_count = sum(confusion[label][label] for label in label_order)
    raw_agreement = agreement_count / n if n else None
    weighted_kappa: float | None = None
    kappa_reason: str | None = "no_labeled_pairs"
    if n:
        ranks = {label: index for index, label in enumerate(label_order)}
        reference_totals = Counter(reference for reference, _ in pairs)
        judge_totals = Counter(judge for _, judge in pairs)
        maximum_distance = max(1, len(label_order) - 1)

        def agreement_weight(left: str, right: str) -> float:
            return 1 - abs(ranks[left] - ranks[right]) / maximum_distance

        observed = sum(
            agreement_weight(reference, judge) * count
            for reference, row in confusion.items()
            for judge, count in row.items()
        ) / n
        expected = sum(
            agreement_weight(reference, judge)
            * reference_totals[reference]
            * judge_totals[judge]
            for reference in label_order
            for judge in label_order
        ) / (n * n)
        if math.isclose(expected, 1.0):
            kappa_reason = "degenerate_expected_agreement"
        else:
            weighted_kappa = (observed - expected) / (1 - expected)
            kappa_reason = None

    return {
        "analysis_version": ANALYSIS_VERSION,
        "annotation_source": ANNOTATION_SOURCE,
        "source_boundary": {
            "reference_labels": ANNOTATION_SOURCE,
            "reference_is_human": False,
            "human_annotation_claim": False,
            "judge_labels": "supplied_actual_judge_outputs",
        },
        "label_order": list(label_order),
        "pair_count": n,
        "unpaired_count": len(reference_labels) - n,
        "coverage_rate": n / len(reference_labels) if reference_labels else None,
        "confusion_matrix": confusion,
        "confusion_matrix_orientation": "rows=reference, columns=judge",
        "raw_agreement": raw_agreement,
        "weighting": "linear",
        "weighted_kappa": weighted_kappa,
        "weighted_kappa_status": "AVAILABLE" if weighted_kappa is not None else "NOT_AVAILABLE",
        "weighted_kappa_reason": kappa_reason,
        "limitation": (
            "Reference labels are assistant-authored. This checks judge calibration against "
            "that reference and does not establish human ground truth or human agreement."
        ),
    }


def analyze_preference_biases(
    observations: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, Any]:
    """Measure position and length preference only from matching real observations.

    Each supplied observation describes a real pairwise annotation with
    ``pair_id``, ``presentation_order`` (two anonymous item IDs), and
    ``preferred_item_id`` (or ``TIE``).  ``answer_lengths`` maps those item IDs to
    observed lengths.  Position results require at least one counterbalanced item order.
    Missing or non-matching data returns ``NOT_RUN``; it is never inferred from outputs.
    """

    if not observations:
        return _preference_not_run(
            position_reason="no_real_position_swap_observations",
            length_reason="no_real_pairwise_length_observations",
        )

    parsed: list[tuple[str, tuple[str, str], str | None, Mapping[str, object] | None]] = []
    orders: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for observation in observations:
        pair_id = _required_text(observation, "pair_id")
        order = _presentation_order(observation.get("presentation_order"))
        preferred = observation.get("preferred_item_id")
        if preferred is not None and not isinstance(preferred, str):
            raise ValueError("preferred_item_id must be text or null")
        if isinstance(preferred, str) and preferred.strip().upper() == "TIE":
            preferred = None
        lengths = observation.get("answer_lengths")
        if lengths is not None and not isinstance(lengths, Mapping):
            raise ValueError("answer_lengths must be a mapping when supplied")
        parsed.append((pair_id, order, preferred, lengths))
        orders[pair_id].add(order)

    counterbalanced = any(
        (second, first) in pair_orders
        for pair_orders in orders.values()
        for first, second in pair_orders
    )
    first_preferences = 0
    second_preferences = 0
    for _, (first, second), preferred, _ in parsed:
        if preferred is None:
            continue
        if preferred == first:
            first_preferences += 1
        elif preferred == second:
            second_preferences += 1
        else:
            raise ValueError("preferred_item_id must match the presentation order")

    non_tie = first_preferences + second_preferences
    if counterbalanced and non_tie:
        position_interval = jeffreys_interval(first_preferences, non_tie)
        position = {
            "status": "AVAILABLE",
            "reason": None,
            "observation_count": len(parsed),
            "counterbalanced_pair_count": sum(
                len(pair_orders) > 1 for pair_orders in orders.values()
            ),
            "first_position_preference_count": first_preferences,
            "second_position_preference_count": second_preferences,
            "tie_count": len(parsed) - non_tie,
            "first_position_preference_rate": first_preferences / non_tie,
            "jeffreys_interval": _interval_payload(*position_interval),
        }
    else:
        position = {
            "status": "NOT_RUN",
            "reason": (
                "no_counterbalanced_position_data"
                if not counterbalanced
                else "no_non_tie_preferences"
            ),
            "observation_count": len(parsed),
        }

    length_differences: list[float] = []
    for _, (first, second), preferred, lengths in parsed:
        if preferred is None or lengths is None:
            continue
        if first not in lengths or second not in lengths:
            continue
        first_length = _finite_nonnegative(lengths[first], "answer_lengths")
        second_length = _finite_nonnegative(lengths[second], "answer_lengths")
        selected = first_length if preferred == first else second_length
        rejected = second_length if preferred == first else first_length
        length_differences.append(selected - rejected)
    length = (
        {
            "status": "AVAILABLE",
            "reason": None,
            "pair_count": len(length_differences),
            "preferred_minus_rejected_mean": mean(length_differences),
            "direction": "positive means preferred answers are longer",
        }
        if length_differences
        else {
            "status": "NOT_RUN",
            "reason": "no_real_pairwise_length_observations",
            "pair_count": 0,
        }
    )
    return {"position_swap": position, "length_preference": length}


def _normalize_rows(rows: Sequence[Mapping[str, object]], *, k: int) -> list[_Trial]:
    normalized: list[_Trial] = []
    identities: set[tuple[str, str, int]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"row {index} must be a mapping")
        repetition = _repetition(row.get("repetition"))
        status = _required_text(row, "status")
        task_success = row.get("task_success")
        if task_success is not None and type(task_success) is not bool:
            raise ValueError("task_success must be boolean or null")
        if status.strip().upper() in _UNKNOWN_STATUSES and task_success is not None:
            raise ValueError("unknown status must have null task_success")
        trial = _Trial(
            case_key=_required_text(row, "case_key"),
            source_group_id=_required_text(row, "source_group_id"),
            split=_required_text(row, "split"),
            variant=_required_text(row, "variant"),
            repetition=repetition,
            status=status,
            task_success=task_success,
            semantic_score=_optional_number(row.get("semantic_score"), "semantic_score"),
            cost_estimate_cny=_optional_number(
                row.get("cost_estimate_cny"), "cost_estimate_cny", nonnegative=True
            ),
            input_tokens=_optional_nonnegative_integer(
                row.get("input_tokens"), "input_tokens"
            ),
            output_tokens=_optional_nonnegative_integer(
                row.get("output_tokens"), "output_tokens"
            ),
            latency_ms=_optional_number(row.get("latency_ms"), "latency_ms", nonnegative=True),
            input_hash=_optional_text(row.get("input_hash"), "input_hash"),
            output_hash=_optional_text(row.get("output_hash"), "output_hash"),
        )
        if repetition is not None and repetition >= k:
            raise ValueError(f"repetition {repetition} is outside configured k={k}")
        if repetition is not None:
            identity = (trial.variant, trial.case_key, repetition)
            if identity in identities:
                raise ValueError(f"duplicate variant/case/repetition: {identity}")
            identities.add(identity)
        normalized.append(trial)
    return normalized


def _summarize_variant(rows: list[_Trial], *, k: int) -> dict[str, Any]:
    outcomes, literal_cases = _trial_outcomes(rows, k=k)
    reliability = summarize_reliability(outcomes, k=k)
    literal_eligible = len(
        literal_cases.intersection(reliability["case_all_success"])
    )
    raw_known = sum(row.task_success is not None for row in rows)
    raw_success = sum(row.task_success is True for row in rows)
    business_interval = jeffreys_interval(raw_success, raw_known)
    complete_successes = sum(reliability["case_all_success"].values())
    complete_count = reliability["complete_case_count"]
    pass_interval = jeffreys_interval(complete_successes, complete_count)

    coverage = {
        "task_success": _coverage(row.task_success for row in rows),
        "repetition": _coverage(row.repetition for row in rows),
        "semantic_score": _coverage(row.semantic_score for row in rows),
        "cost_estimate_cny": _coverage(row.cost_estimate_cny for row in rows),
        "input_tokens": _coverage(row.input_tokens for row in rows),
        "output_tokens": _coverage(row.output_tokens for row in rows),
        "latency_ms": _coverage(row.latency_ms for row in rows),
        "input_hash": _coverage(row.input_hash for row in rows),
        "output_hash": _coverage(row.output_hash for row in rows),
        "reliability_outcome": _coverage(
            [_reliability_success(row) for row in rows]
        ),
    }
    literal_value = reliability["literal_consistency_at_k"]
    return {
        "variant": rows[0].variant,
        "trial_count": len(rows),
        "case_count": reliability["case_count"],
        "coverage": coverage,
        "status_counts": dict(sorted(Counter(row.status for row in rows).items())),
        "business_success": {
            "success_count": raw_success,
            "known_trial_count": raw_known,
            "unknown_trial_count": len(rows) - raw_known,
            "success_rate": raw_success / raw_known if raw_known else None,
            "jeffreys_interval": _optional_interval_payload(business_interval),
        },
        "complete_case_reliability": {
            "complete_case_count": complete_count,
            "incomplete_case_count": reliability["incomplete_case_count"],
            "complete_case_rate": reliability["complete_case_rate"],
            "pass_hat_k": reliability["pass_hat_k"],
            "pass_hat_k_all_cases": reliability["pass_hat_k_all_cases"],
            "pass_hat_k_jeffreys_interval": _optional_interval_payload(pass_interval),
            "trial_variance": reliability["trial_variance"],
            "case_success_rates": reliability["case_success_rates"],
        },
        "literal_consistency": {
            "status": "AVAILABLE" if literal_eligible else "NOT_AVAILABLE",
            "value": literal_value,
            "eligible_complete_case_count": literal_eligible,
            "reason": None if literal_eligible else "input_and_output_hashes_incomplete",
            "definition": (
                "At fixed k, a complete successful case counts only when every trial has "
                "input_hash and output_hash and one identical output_hash."
            ),
        },
        "observed_totals": {
            field: _metric_total([getattr(row, field) for row in rows])
            for field in _NUMERIC_FIELDS
        },
    }


def _trial_outcomes(
    rows: list[_Trial], *, k: int
) -> tuple[list[TrialOutcome], set[str]]:
    by_case: dict[str, list[_Trial]] = defaultdict(list)
    for row in rows:
        by_case[row.case_key].append(row)

    literal_cases: set[str] = set()
    for case_key, case_rows in by_case.items():
        if all(row.input_hash and row.output_hash for row in case_rows):
            input_hashes = {row.input_hash for row in case_rows if row.input_hash}
            if len(input_hashes) == 1:
                literal_cases.add(case_key)

    outcomes: list[TrialOutcome] = []
    for row in rows:
        hash_complete = row.case_key in literal_cases
        outcomes.append(
            TrialOutcome(
                case_key=row.case_key,
                trial_index=row.repetition if row.repetition is not None else -1,
                success=_reliability_success(row),
                output_hash=row.output_hash if hash_complete else None,
            )
        )
    return outcomes, literal_cases


def _compare_variants(
    rows: list[_Trial],
    *,
    k: int,
    baseline_variant: str,
    candidate_variant: str,
    bootstrap_count: int,
    seed: int,
) -> dict[str, Any]:
    by_variant_case: dict[str, dict[str, list[_Trial]]] = {
        baseline_variant: defaultdict(list),
        candidate_variant: defaultdict(list),
    }
    for row in rows:
        if row.variant in by_variant_case:
            by_variant_case[row.variant][row.case_key].append(row)

    all_cases = sorted(
        set(by_variant_case[baseline_variant])
        | set(by_variant_case[candidate_variant])
    )
    metrics = {
        field: _paired_metric(
            all_cases,
            baseline=by_variant_case[baseline_variant],
            candidate=by_variant_case[candidate_variant],
            k=k,
            field=field,
            bootstrap_count=bootstrap_count,
            seed=seed,
        )
        for field in _COMPARISON_FIELDS
    }
    return {
        "baseline_variant": baseline_variant,
        "candidate_variant": candidate_variant,
        "direction": "candidate_minus_baseline",
        "metrics": metrics,
    }


def _paired_metric(
    cases: Sequence[str],
    *,
    baseline: Mapping[str, list[_Trial]],
    candidate: Mapping[str, list[_Trial]],
    k: int,
    field: str,
    bootstrap_count: int,
    seed: int,
) -> dict[str, Any]:
    group_deltas: dict[str, list[float]] = defaultdict(list)
    unpaired: dict[str, list[str]] = defaultdict(list)
    for case_key in cases:
        baseline_rows = baseline.get(case_key, [])
        candidate_rows = candidate.get(case_key, [])
        reason = _pair_identity_reason(baseline_rows, candidate_rows)
        if reason is not None:
            unpaired[reason].append(case_key)
            continue
        baseline_value, baseline_reason = _case_metric_value(baseline_rows, k=k, field=field)
        if baseline_reason is not None:
            unpaired[f"baseline:{baseline_reason}"].append(case_key)
            continue
        candidate_value, candidate_reason = _case_metric_value(candidate_rows, k=k, field=field)
        if candidate_reason is not None:
            unpaired[f"candidate:{candidate_reason}"].append(case_key)
            continue
        assert baseline_rows[0].source_group_id == candidate_rows[0].source_group_id
        group_deltas[baseline_rows[0].source_group_id].append(
            candidate_value - baseline_value
        )

    group_means = [mean(values) for _, values in sorted(group_deltas.items())]
    interval = cluster_bootstrap_interval(
        group_means,
        bootstrap_count=bootstrap_count,
        seed=seed,
    )
    case_count = sum(len(values) for values in group_deltas.values())
    return {
        "status": "AVAILABLE" if group_means else "NOT_AVAILABLE",
        "reason": None if group_means else "no_pairable_cases",
        "estimate": mean(group_means) if group_means else None,
        "interval": _optional_interval_payload(interval),
        "interval_method": "source_group_cluster_bootstrap",
        "estimate_weighting": "equal_source_group",
        "source_group_count": len(group_deltas),
        "case_count": case_count,
        "trial_unit_count": sum(
            len(baseline[case_key]) + len(candidate[case_key])
            for case_key in cases
            if case_key not in {case for values in unpaired.values() for case in values}
        ),
        "bootstrap_count": bootstrap_count,
        "seed": seed,
        "unpaired_reasons": {
            reason: sorted(case_keys) for reason, case_keys in sorted(unpaired.items())
        },
        "unit": "case",
    }


def _pair_identity_reason(
    baseline_rows: list[_Trial], candidate_rows: list[_Trial]
) -> str | None:
    if not baseline_rows:
        return "baseline_variant_missing"
    if not candidate_rows:
        return "candidate_variant_missing"
    baseline_groups = {row.source_group_id for row in baseline_rows}
    candidate_groups = {row.source_group_id for row in candidate_rows}
    if len(baseline_groups) != 1:
        return "baseline_source_group_conflict"
    if len(candidate_groups) != 1:
        return "candidate_source_group_conflict"
    if baseline_groups != candidate_groups:
        return "source_group_mismatch"
    baseline_splits = {row.split for row in baseline_rows}
    candidate_splits = {row.split for row in candidate_rows}
    if len(baseline_splits) != 1:
        return "baseline_split_conflict"
    if len(candidate_splits) != 1:
        return "candidate_split_conflict"
    if baseline_splits != candidate_splits:
        return "split_mismatch"
    return None


def _case_metric_value(
    rows: list[_Trial], *, k: int, field: str
) -> tuple[float, str | None]:
    repetitions = [row.repetition for row in rows]
    if any(repetition is None for repetition in repetitions):
        return 0.0, "missing_repetition"
    if set(repetitions) != set(range(k)):
        return 0.0, "incomplete_repetitions"
    if field == "business_pass_hat_k":
        outcomes = [row.task_success for row in rows]
        if any(outcome is None for outcome in outcomes):
            return 0.0, "unknown_task_success"
        return float(all(outcome is True for outcome in outcomes)), None
    values = [getattr(row, field) for row in rows]
    if any(value is None for value in values):
        return 0.0, f"unknown_{field}"
    return float(mean(value for value in values if value is not None)), None


def _coverage(values: Iterable[object]) -> dict[str, Any]:
    materialized = list(values)
    known = sum(value is not None for value in materialized)
    total = len(materialized)
    return {
        "known_count": known,
        "unknown_count": total - known,
        "known_rate": known / total if total else None,
        "unknown_rate": (total - known) / total if total else None,
    }


def _metric_total(values: Sequence[float | int | None]) -> dict[str, Any]:
    known = [value for value in values if value is not None]
    return {
        "known_count": len(known),
        "unknown_count": len(values) - len(known),
        "sum": sum(known) if known else None,
        "mean": mean(known) if known else None,
    }


def _interval_payload(lower: float, upper: float) -> dict[str, float]:
    return {"lower": lower, "upper": upper}


def _optional_interval_payload(
    interval: tuple[float, float] | None,
) -> dict[str, float] | None:
    return _interval_payload(*interval) if interval is not None else None


def _preference_not_run(*, position_reason: str, length_reason: str) -> dict[str, Any]:
    return {
        "position_swap": {"status": "NOT_RUN", "reason": position_reason},
        "length_preference": {"status": "NOT_RUN", "reason": length_reason},
    }


def _presentation_order(value: object) -> tuple[str, str]:
    if isinstance(value, str):
        order = [value] if not value else [value]
    elif isinstance(value, Sequence):
        order = list(value)
    else:
        raise ValueError("presentation_order must contain two item IDs")
    if len(order) != 2 or any(not isinstance(item, str) or not item for item in order):
        raise ValueError("presentation_order must contain two non-empty item IDs")
    if order[0] == order[1]:
        raise ValueError("presentation_order item IDs must differ")
    return order[0], order[1]


def _redact_identities(value: str, tokens: set[str]) -> str:
    redactable = sorted((token for token in tokens if len(token) >= 3), key=len, reverse=True)
    if not redactable:
        return value
    pattern = re.compile("|".join(re.escape(token) for token in redactable), re.IGNORECASE)
    return pattern.sub("[identity hidden]", value)


def _blind_sort_key(row: Mapping[str, object], answer_field: str) -> tuple[str, int, str, str]:
    repetition = row.get("repetition")
    repetition_order = (
        repetition
        if isinstance(repetition, int) and not isinstance(repetition, bool)
        else -1
    )
    answer = row.get(answer_field)
    return (
        _required_text(row, "case_key"),
        repetition_order,
        _required_text(row, "variant"),
        answer if isinstance(answer, str) else "",
    )


def _trial_sort_key(row: _Trial) -> tuple[str, int, str, str]:
    return (
        row.case_key,
        row.repetition if row.repetition is not None else -1,
        row.variant,
        row.status,
    )


def _reliability_success(row: _Trial) -> bool | None:
    if row.repetition is None or row.status.strip().upper() in _UNKNOWN_STATUSES:
        return None
    return row.task_success


def _required_text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text or null")
    return value.strip()


def _repetition(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("repetition must be a non-negative integer or null")
    return value


def _optional_number(
    value: object, field: str, *, nonnegative: bool = False
) -> float | None:
    if value is None:
        return None
    number = _finite_nonnegative(value, field) if nonnegative else _finite_number(value, field)
    return float(number)


def _optional_nonnegative_integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer or null")
    return value


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric or null")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite or null")
    return number


def _finite_nonnegative(value: object, field: str) -> float:
    number = _finite_number(value, field)
    if number < 0:
        raise ValueError(f"{field} must be non-negative")
    return number


def _validate_k(k: int) -> None:
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")


def _validate_bootstrap(bootstrap_count: int, seed: int) -> None:
    if isinstance(bootstrap_count, bool) or not isinstance(bootstrap_count, int):
        raise ValueError("bootstrap_count must be an integer")
    if bootstrap_count < 1:
        raise ValueError("bootstrap_count must be positive")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")


__all__ = [
    "ANALYSIS_VERSION",
    "ANNOTATION_SOURCE",
    "analyze_preference_biases",
    "analyze_trials",
    "build_blind_annotation_pack",
    "calibrate_judge",
]
