"""Offline audit statistics over an explicit plan and independent human labels.

These additive metrics do not reinterpret persisted historical evaluator results.
No answer text, model output or expected label is treated as an observation here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from packages.evaluation.metrics import MetricStatus, MetricValue, percentile

VERSION = "quality-audit-v1"


def _optional_bool(value: object) -> None:
    if value is not None and type(value) is not bool:
        raise ValueError("AUDIT_BOOLEAN_REQUIRED")


@dataclass(frozen=True)
class TrialObservation:
    case_id: str
    variant: str
    repetition: int
    business_success: bool | None = None
    refused: bool | None = None
    latency_ms: float | None = None

    def __post_init__(self):
        if not self.case_id or not self.variant or type(self.repetition) is not int:
            raise ValueError("INVALID_TRIAL_IDENTITY")
        if self.repetition not in {0, 1, 2}:
            raise ValueError("THREE_INDEPENDENT_TRIALS_REQUIRED")
        _optional_bool(self.business_success)
        _optional_bool(self.refused)
        if self.latency_ms is not None and (
            type(self.latency_ms) not in {int, float}
            or not math.isfinite(self.latency_ms)
            or self.latency_ms < 0
        ):
            raise ValueError("INVALID_TRIAL_LATENCY")


def _rate(name, numerator, denominator, known, **details):
    return MetricValue(
        name,
        MetricStatus.AVAILABLE if known and denominator else MetricStatus.NOT_AVAILABLE,
        numerator / denominator if known and denominator else None,
        known,
        numerator,
        denominator,
        reason=None if known and denominator else "no_observed_denominator",
        evaluator_version=VERSION,
        details=details,
    ).to_dict()


def trial_metrics(
    case_ids: Sequence[str],
    variants: Sequence[str],
    observations: Sequence[TrialObservation],
    *,
    answer_available: Mapping[str, bool | None] | None = None,
) -> dict[str, dict]:
    """Every planned case contributes three trials, even if no result was received."""
    if (
        not case_ids
        or not variants
        or len(set(case_ids)) != len(case_ids)
        or len(set(variants)) != len(variants)
        or any(not isinstance(x, str) or not x for x in (*case_ids, *variants))
    ):
        raise ValueError("INVALID_AUDIT_PLAN")
    availability = dict(answer_available or {})
    if set(availability) - set(case_ids):
        raise ValueError("UNPLANNED_ANSWER_LABEL")
    for value in availability.values():
        _optional_bool(value)
    indexed = {}
    for row in observations:
        key = (row.case_id, row.variant, row.repetition)
        if row.case_id not in case_ids or row.variant not in variants:
            raise ValueError("UNPLANNED_TRIAL")
        if key in indexed:
            raise ValueError("DUPLICATE_TRIAL")
        indexed[key] = row
    output = {}
    for variant in variants:
        rows = [indexed.get((case, variant, rep)) for case in case_ids for rep in range(3)]
        complete = successful = 0
        for case in case_ids:
            results = [indexed.get((case, variant, rep)) for rep in range(3)]
            labels = [r.business_success if r else None for r in results]
            complete += all(value is not None for value in labels)
            successful += all(value is True for value in labels)
        known = sum(row is not None and row.business_success is not None for row in rows)
        metrics = {
            "all_3_trials_success_rate": _rate(
                "all_3_trials_success_rate",
                successful,
                len(case_ids),
                complete,
                planned_task_count=len(case_ids),
                complete_task_count=complete,
                incomplete_task_count=len(case_ids) - complete,
                all_planned_trials_present=complete == len(case_ids),
            ),
            "business_trial_success_rate": _rate(
                "business_trial_success_rate",
                sum(row is not None and row.business_success is True for row in rows),
                len(rows),
                known,
                unknown_count=len(rows) - known,
            ),
        }
        for prefix, predicate in (
            ("all_trial", lambda row: True),
            ("successful_trial", lambda row: row.business_success is True),
        ):
            values = [
                row.latency_ms
                for row in rows
                if row and row.latency_ms is not None and predicate(row)
            ]
            for suffix, rank in (("p50_ms", 0.5), ("p95_ms", 0.95)):
                name = f"{prefix}_latency_{suffix}"
                metrics[name] = MetricValue(
                    name,
                    MetricStatus.AVAILABLE if values else MetricStatus.NOT_AVAILABLE,
                    percentile(values, rank) if values else None,
                    len(values),
                    reason=None if values else "latency_not_observed",
                    evaluator_version=VERSION,
                    details={"planned_trial_count": len(rows)},
                ).to_dict()
        for available, name in (
            (False, "correct_refusal_rate"),
            (True, "incorrect_refusal_rate"),
        ):
            applicable = [
                indexed.get((case, variant, rep))
                for case in case_ids
                if availability.get(case) is available
                for rep in range(3)
            ]
            refusal_known = sum(r is not None and r.refused is not None for r in applicable)
            metrics[name] = _rate(
                name,
                sum(r is not None and r.refused is True for r in applicable),
                len(applicable),
                refusal_known,
                unknown_count=len(applicable) - refusal_known,
                missing_answer_label_case_count=sum(availability.get(c) is None for c in case_ids),
            )
        output[variant] = metrics
    return output


@dataclass(frozen=True)
class ClaimReview:
    claim_id: str
    reviewer_id: str
    supported: bool | None

    def __post_init__(self):
        if not self.claim_id or not self.reviewer_id:
            raise ValueError("HUMAN_REVIEW_IDENTITY_REQUIRED")
        _optional_bool(self.supported)


def claim_metrics(planned_claim_ids: Sequence[str], reviews: Sequence[ClaimReview]) -> dict:
    if len(set(planned_claim_ids)) != len(planned_claim_ids):
        raise ValueError("DUPLICATE_PLANNED_CLAIM")
    indexed = {}
    for review in reviews:
        if review.claim_id not in planned_claim_ids or review.claim_id in indexed:
            raise ValueError("UNPLANNED_OR_DUPLICATE_CLAIM_REVIEW")
        indexed[review.claim_id] = review
    verified = [r for r in reviews if r.supported is not None]
    return {
        "claim_support_rate": _rate(
            "claim_support_rate",
            sum(r.supported is True for r in verified),
            len(verified),
            len(verified),
            unable_to_verify_count=sum(r.supported is None for r in reviews),
            unreviewed_count=len(planned_claim_ids) - len(reviews),
        ),
        "claim_review_coverage": _rate(
            "claim_review_coverage",
            len(verified),
            len(planned_claim_ids),
            len(reviews),
            planned_claim_count=len(planned_claim_ids),
        ),
    }


@dataclass(frozen=True)
class JudgeReview:
    sample_id: str
    status: str
    judge_label: bool | None
    human_label: bool | None
    reviewer_id: str | None

    def __post_init__(self):
        if not self.sample_id or self.status not in {"SCORED", "UNPARSEABLE", "FAILED"}:
            raise ValueError("INVALID_JUDGE_SAMPLE")
        _optional_bool(self.judge_label)
        _optional_bool(self.human_label)
        if (self.status == "SCORED") != (self.judge_label is not None):
            raise ValueError("JUDGE_LABEL_STATUS_MISMATCH")
        if self.human_label is not None and not self.reviewer_id:
            raise ValueError("HUMAN_REVIEW_IDENTITY_REQUIRED")


def judge_metrics(reviews: Sequence[JudgeReview]) -> dict:
    if len({r.sample_id for r in reviews}) != len(reviews):
        raise ValueError("DUPLICATE_JUDGE_SAMPLE")
    pairs = [r for r in reviews if r.status == "SCORED" and r.human_label is not None]
    cells = {name: 0 for name in ("TP", "TN", "FP", "FN")}
    for row in pairs:
        cells[
            ("TP" if row.human_label else "FP")
            if row.judge_label
            else ("FN" if row.human_label else "TN")
        ] += 1
    agreement = cells["TP"] + cells["TN"]
    result = {
        "judge_agreement": _rate(
            "judge_agreement",
            agreement,
            len(pairs),
            len(pairs),
            confusion_matrix=cells,
            unpaired_count=len(reviews) - len(pairs),
        ),
        "judge_parse_failure_rate": _rate(
            "judge_parse_failure_rate",
            sum(r.status == "UNPARSEABLE" for r in reviews),
            len(reviews),
            len(reviews),
            failed_count=sum(r.status == "FAILED" for r in reviews),
        ),
    }
    kappa, reason = None, "fewer_than_20_human_judge_pairs"
    if len(pairs) >= 20:
        n = len(pairs)
        expected = (
            (cells["TP"] + cells["FP"]) * (cells["TP"] + cells["FN"])
            + (cells["TN"] + cells["FN"]) * (cells["TN"] + cells["FP"])
        ) / n**2
        reason = "degenerate_label_distribution" if expected == 1 else None
        if reason is None:
            kappa = (agreement / n - expected) / (1 - expected)
    result["judge_kappa"] = MetricValue(
        "judge_kappa",
        MetricStatus.AVAILABLE if kappa is not None else MetricStatus.NOT_AVAILABLE,
        kappa,
        len(pairs),
        reason=reason,
        evaluator_version=VERSION,
    ).to_dict()
    return result
