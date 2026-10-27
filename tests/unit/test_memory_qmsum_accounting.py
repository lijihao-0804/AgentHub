"""Net benefit includes the summary call and never treats missing cost as zero."""

from decimal import Decimal

from benchmarks.evaluation.memory_qmsum_accounting import account_pressure_pairs
from benchmarks.evaluation.memory_qmsum_protocol import (
    QMSumArm,
    QMSumArmRecord,
    QMSumCallMeasurements,
)
from benchmarks.evaluation.memory_qmsum_summary_runner import QMSumSummaryObservation
from tests.unit.test_memory_qmsum_protocol import _plan, _review, _source, _validated_candidate


def _case():
    source = _source()
    qid = source.questions[0].question_id
    candidate = _validated_candidate(qid)
    plan = _plan(source, compressed_candidates={qid: candidate})
    case = next(case for case in plan.cases if case.question_id == qid)
    arms = tuple(
        QMSumArmRecord(
            qid,
            arm.arm,
            "observed answer",
            QMSumCallMeasurements(
                7000 if arm.arm is QMSumArm.B else 3500,
                10,
                wall_time_ms=10,
                cost_cny=Decimal("0.02") if arm.arm is QMSumArm.B else Decimal("0.01"),
                cost_identity="test-pricing",
            ),
            review=_review(qid, arm.arm),
            summary_candidate_id=arm.compressed_candidate_id,
        )
        for arm in case.arms
    )
    summary = QMSumSummaryObservation(
        qid,
        "VALID",
        {
            "cost_identity": "test-pricing",
            "conservative_cost_cny": "0.02",
            "input_tokens": 7000,
            "output_tokens": 1000,
        },
        candidate=candidate,
    )
    return plan, arms, summary


def test_summary_cost_turns_an_answer_input_reduction_into_net_loss():
    plan, arms, summary = _case()
    report = account_pressure_pairs(plan, arms, (summary,))
    assert report.pressure_eligible_pairs == report.fully_priced_pairs == 1
    assert report.baseline_cost_cny == Decimal("0.02")
    assert report.candidate_cost_cny == Decimal("0.03")
    assert report.candidate_cost_change_ratio == Decimal("0.5")
    assert report.baseline_total_tokens == 7010
    assert report.candidate_total_tokens == 11510


def test_missing_summary_price_is_unknown_and_failed_attempt_is_counted():
    plan, arms, summary = _case()
    summary = QMSumSummaryObservation(
        summary.question_id,
        summary.status,
        {"cost_identity": "test-pricing", "input_tokens": 7000, "output_tokens": 1000},
        candidate=summary.candidate,
    )
    failed = QMSumSummaryObservation(
        plan.cases[1].question_id,
        "INVALID",
        {"conservative_cost_cny": "0.003"},
    )
    report = account_pressure_pairs(plan, arms, (summary, failed))
    assert report.pressure_eligible_pairs == 1 and report.fully_priced_pairs == 0
    assert report.baseline_cost_cny is None and report.candidate_cost_cny is None
    assert report.candidate_cost_change_ratio is None
    assert report.unpaired_summary_attempts == 1
    assert report.unpaired_summary_cost_cny == Decimal("0.003")


def test_actual_hard_pressure_cannot_enter_net_savings():
    plan, arms, summary = _case()
    changed = tuple(
        QMSumArmRecord(
            row.question_id,
            row.arm,
            row.output,
            QMSumCallMeasurements(
                8415 if row.arm is QMSumArm.B else 3500,
                10,
                wall_time_ms=10,
                cost_cny=row.measurements.cost_cny,
                cost_identity=row.measurements.cost_identity,
            ),
            review=row.review,
            summary_candidate_id=row.summary_candidate_id,
        )
        for row in arms
    )
    report = account_pressure_pairs(plan, changed, (summary,))
    assert report.pressure_eligible_pairs == 0
    assert report.candidate_cost_change_ratio is None
    assert report.unpaired_summary_cost_cny == Decimal("0.02")
