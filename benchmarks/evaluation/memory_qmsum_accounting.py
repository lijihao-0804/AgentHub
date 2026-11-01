"""Account for the extra summary call in each valid QMSum comparison."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from benchmarks.evaluation.memory_qmsum_protocol import (
    QMSumArm,
    QMSumArmRecord,
    QMSumDispatchPlan,
    QMSumObservationStatus,
    QMSumProtocolError,
    record_observations,
)
from benchmarks.evaluation.memory_qmsum_summary_runner import QMSumSummaryObservation


@dataclass(frozen=True, slots=True)
class QMSumNetAccounting:
    pressure_eligible_pairs: int
    fully_priced_pairs: int
    baseline_cost_cny: Decimal | None
    candidate_cost_cny: Decimal | None
    candidate_cost_change_ratio: Decimal | None
    baseline_total_tokens: int | None
    candidate_total_tokens: int | None
    unpaired_summary_attempts: int
    unpaired_summary_cost_cny: Decimal | None


def account_pressure_pairs(
    plan: QMSumDispatchPlan,
    arms: Sequence[QMSumArmRecord],
    summaries: Sequence[QMSumSummaryObservation],
) -> QMSumNetAccounting:
    """Count B versus summary+C, retaining unknowns instead of guessing zero.

    Only actually observed, reviewed-answerable B/C pairs inside the 70% to
    below-85% provider-usage band enter this comparison. C quality is a
    separate promotion gate; a failing C is never removed to improve cost.
    """
    rows = record_observations(plan, arms)
    by_question: dict[str, dict[QMSumArm, QMSumArmRecord]] = {}
    for row in rows:
        by_question.setdefault(row.question_id, {})[row.arm] = row
    summary_rows = tuple(summaries)
    if any(not isinstance(row, QMSumSummaryObservation) for row in summary_rows):
        raise TypeError("summary accounting requires summary observations")
    if len({row.question_id for row in summary_rows}) != len(summary_rows):
        raise QMSumProtocolError("summary accounting requires one observation per question")
    by_summary = {row.question_id: row for row in summary_rows}
    cases = {case.question_id: case for case in plan.cases}
    eligible: list[tuple[QMSumArmRecord, QMSumArmRecord]] = []
    for question_id, pair in by_question.items():
        if set(pair) != {QMSumArm.B, QMSumArm.C}:
            continue
        b, c = pair[QMSumArm.B], pair[QMSumArm.C]
        if any(row.status is not QMSumObservationStatus.OBSERVED for row in (b, c)):
            continue
        if b.review is None or not b.review.task_qualified:
            continue
        b_input, c_input = b.measurements.input_tokens, c.measurements.input_tokens
        if b_input is None or c_input is None:
            continue
        b_arm = next(arm for arm in cases[question_id].arms if arm.arm is QMSumArm.B)
        available = b_arm.admitted.usage.available_input
        if not (
            70 * available <= 100 * b_input < 85 * available and 100 * c_input < 85 * available
        ):
            continue
        eligible.append((b, c))

    priced: list[tuple[Decimal, Decimal, int, int]] = []
    paired_summary_ids: set[str] = set()
    for b, c in eligible:
        summary = by_summary.get(b.question_id)
        if summary is None or summary.status != "VALID" or summary.candidate is None:
            continue
        if summary.candidate.candidate_id != c.summary_candidate_id:
            continue
        paired_summary_ids.add(b.question_id)
        identity = b.measurements.cost_identity
        if (
            not identity
            or identity != c.measurements.cost_identity
            or identity != summary.measurements.get("cost_identity")
        ):
            continue
        summary_cost = _cost(summary.measurements.get("conservative_cost_cny"))
        b_cost, c_cost = b.measurements.cost_cny, c.measurements.cost_cny
        summary_input = _tokens(summary.measurements.get("input_tokens"))
        summary_output = _tokens(summary.measurements.get("output_tokens"))
        if (
            summary_cost is None
            or b_cost is None
            or c_cost is None
            or summary_input is None
            or summary_output is None
            or b.measurements.output_tokens is None
            or c.measurements.output_tokens is None
        ):
            continue
        assert b.measurements.input_tokens is not None and c.measurements.input_tokens is not None
        priced.append(
            (
                b_cost,
                summary_cost + c_cost,
                b.measurements.input_tokens + b.measurements.output_tokens,
                summary_input
                + summary_output
                + c.measurements.input_tokens
                + c.measurements.output_tokens,
            )
        )

    complete = bool(eligible) and len(priced) == len(eligible)
    baseline_cost = sum((row[0] for row in priced), Decimal(0)) if complete else None
    candidate_cost = sum((row[1] for row in priced), Decimal(0)) if complete else None
    change = (
        (candidate_cost - baseline_cost) / baseline_cost
        if baseline_cost is not None and baseline_cost > 0 and candidate_cost is not None
        else None
    )
    unpaired = [
        row
        for row in summary_rows
        if row.question_id not in paired_summary_ids and row.status != "NOT_DISPATCHED"
    ]
    unpaired_costs = [_cost(row.measurements.get("conservative_cost_cny")) for row in unpaired]
    unpaired_total = (
        sum((cost for cost in unpaired_costs if cost is not None), Decimal(0))
        if all(cost is not None for cost in unpaired_costs)
        else None
    )
    return QMSumNetAccounting(
        pressure_eligible_pairs=len(eligible),
        fully_priced_pairs=len(priced),
        baseline_cost_cny=baseline_cost,
        candidate_cost_cny=candidate_cost,
        candidate_cost_change_ratio=change,
        baseline_total_tokens=sum(row[2] for row in priced) if complete else None,
        candidate_total_tokens=sum(row[3] for row in priced) if complete else None,
        unpaired_summary_attempts=len(unpaired),
        unpaired_summary_cost_cny=unpaired_total,
    )


def _cost(value: object) -> Decimal | None:
    if not isinstance(value, (str, Decimal)):
        return None
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _tokens(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None
