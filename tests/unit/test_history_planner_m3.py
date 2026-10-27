"""M3 pure token-history planner contract and regression tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from packages.agent_runtime.history_planner import (
    MANDATORY_OVERFLOW_CODE,
    DecisionReason,
    EligibilityReason,
    HistoryCategory,
    HistoryPlanContractError,
    HistoryScope,
    HistorySourceGroup,
    HistoryWorkLimit,
    InsufficiencyCode,
    PlanPressure,
    PlanReason,
    SummaryCoverage,
    SummaryEligibility,
    plan_history,
)
from packages.core.errors.exceptions import AgentHubError

SCOPE = HistoryScope(
    workspace_id="workspace-1",
    thread_id="thread-1",
    permission_snapshot="permission-7",
)


class CountingCounter:
    counter_id = "test_whole_group_counter"
    counter_version = 1

    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.calls: list[str] = []

    def count(self, group: HistorySourceGroup) -> int:
        self.calls.append(group.group_id)
        return self.counts[group.group_id]


def _group(
    group_id: str,
    *,
    category: HistoryCategory | str | None,
    eligibility: SummaryEligibility | str = SummaryEligibility.INELIGIBLE,
    sequence: int = 1,
    watermark: int = 10,
    coverage: tuple[SummaryCoverage, ...] = (),
    verified: bool = False,
    verification_ref: str | None = None,
) -> HistorySourceGroup:
    return HistorySourceGroup(
        group_id=group_id,
        scope=SCOPE,
        revision=f"{group_id}-revision",
        sequence=sequence,
        watermark=watermark,
        exchange_id=f"{group_id}-exchange",
        original_ref=f"thread://{group_id}",
        count_version=1,
        category=category,
        summary_eligibility=eligibility,
        coverage=coverage,
        verified=verified,
        verification_ref=verification_ref,
    )


def _summary(
    group_id: str,
    *,
    sequence: int,
    covered: tuple[HistorySourceGroup, ...],
) -> HistorySourceGroup:
    return _group(
        group_id,
        category=HistoryCategory.VERIFIED_SUMMARY,
        sequence=sequence,
        coverage=tuple(SummaryCoverage(item.group_id, item.revision) for item in covered),
        verified=True,
        verification_ref=f"publication://{group_id}",
    )


def test_groups_are_immutable_and_unknown_eligibility_stays_keep_exact() -> None:
    unknown = _group(
        "unknown",
        category=None,
        eligibility=SummaryEligibility.UNKNOWN,
    )
    counter = CountingCounter({"unknown": 12})

    plan = plan_history(
        (unknown,),
        scope=SCOPE,
        frozen_watermark=10,
        counter=counter,
        b_work_tokens=100,
    )

    assert plan.decisions[0].category is HistoryCategory.KEEP_EXACT
    assert plan.decisions[0].eligibility_reason is EligibilityReason.UNKNOWN_CATEGORY_KEEP_EXACT
    assert plan.decisions[0].retained
    assert plan.decisions[0].reason is DecisionReason.RETAINED_KEEP_EXACT
    with pytest.raises(FrozenInstanceError):
        unknown.group_id = "changed"  # type: ignore[misc]


def test_pressure_is_measured_before_legacy_ten_turn_cut() -> None:
    groups = tuple(
        _group(
            f"turn-{index:02d}", category=HistoryCategory.KEEP_EXACT, sequence=index, watermark=12
        )
        for index in range(12)
    )
    counter = CountingCounter({item.group_id: 2_000 for item in groups})
    plan = plan_history(
        groups, scope=SCOPE, frozen_watermark=12, counter=counter, b_work_tokens=32_768
    )
    assert 10 * 2_000 < plan.thresholds.soft_tokens <= plan.pre_trim_tokens
    assert plan.pre_trim_pressure is PlanPressure.SOFT
    assert plan.measurement_complete and len(plan.retained_group_ids) == 12


def test_a_source_sequence_cannot_exceed_its_frozen_watermark() -> None:
    group = _group("future", category=HistoryCategory.KEEP_EXACT, sequence=11, watermark=10)
    with pytest.raises(HistoryPlanContractError):
        plan_history(
            (group,),
            scope=SCOPE,
            frozen_watermark=10,
            counter=CountingCounter({"future": 1}),
            b_work_tokens=100,
        )


def test_watermark_and_counter_must_match_the_frozen_contract() -> None:
    for group in (
        _group("equal", category=HistoryCategory.KEEP_EXACT, sequence=10, watermark=10),
        _group("different", category=HistoryCategory.KEEP_EXACT, sequence=1, watermark=9),
    ):
        with pytest.raises(HistoryPlanContractError):
            plan_history(
                (group,),
                scope=SCOPE,
                frozen_watermark=10,
                counter=CountingCounter({group.group_id: 1}),
                b_work_tokens=100,
            )
    with pytest.raises(HistoryPlanContractError):
        plan_history(
            (),
            scope=SCOPE,
            frozen_watermark=10,
            counter=CountingCounter({}),
            b_work_tokens=100,
            expected_counter_id="approx_language_v1",
            expected_counter_version=1,
        )


def test_unknown_summary_qualification_promotes_optional_to_keep_exact() -> None:
    optional_with_unknown_qualification = _group(
        "maybe-summarizable",
        category=HistoryCategory.OPTIONAL,
        eligibility=SummaryEligibility.UNKNOWN,
    )
    plan = plan_history(
        (optional_with_unknown_qualification,),
        scope=SCOPE,
        frozen_watermark=10,
        counter=CountingCounter({"maybe-summarizable": 10}),
        b_work_tokens=100,
    )

    decision = plan.decisions[0]
    assert decision.category is HistoryCategory.KEEP_EXACT
    assert decision.eligibility_reason is EligibilityReason.UNKNOWN_SUMMARY_ELIGIBILITY_KEEP_EXACT
    assert decision.summary_eligibility is SummaryEligibility.UNKNOWN


@pytest.mark.parametrize(
    ("tokens", "pressure"),
    [
        (69, PlanPressure.NONE),
        (70, PlanPressure.SOFT),
        (84, PlanPressure.SOFT),
        (85, PlanPressure.HARD),
    ],
)
def test_70_85_thresholds_are_integer_boundaries(
    tokens: int,
    pressure: PlanPressure,
) -> None:
    plan = plan_history(
        (_group("optional", category=HistoryCategory.OPTIONAL),),
        scope=SCOPE,
        frozen_watermark=10,
        counter=CountingCounter({"optional": tokens}),
        b_work_tokens=100,
    )

    assert plan.thresholds.soft_percent == 70
    assert plan.thresholds.hard_percent == 85
    assert plan.thresholds.target_percent == 55
    assert plan.thresholds.soft_tokens == 70
    assert plan.thresholds.hard_tokens == 85
    assert plan.thresholds.target_tokens == 55
    assert plan.pre_trim_pressure is pressure
    if pressure is PlanPressure.HARD:
        assert not plan.ready_for_dispatch
        assert plan.insufficiencies[-1].code is (
            InsufficiencyCode.POST_TRIM_PLANNED_AT_OR_ABOVE_HARD_LIMIT
        )


def test_planned_is_pre_trim_n_plus_only_unaccounted_growth() -> None:
    mandatory = _group("mandatory", category=HistoryCategory.MANDATORY, sequence=1)
    optional = _group("optional", category=HistoryCategory.OPTIONAL, sequence=2)
    summary = _summary("summary", sequence=3, covered=(optional,))
    counter = CountingCounter({"mandatory": 10, "optional": 30, "summary": 8})

    plan = plan_history(
        (mandatory, optional, summary),
        scope=SCOPE,
        frozen_watermark=10,
        counter=counter,
        b_work_tokens=100,
        unaccounted_growth_tokens=7,
    )

    assert plan.pre_trim_tokens == 40
    assert plan.unaccounted_growth_tokens == 7
    assert plan.planned_tokens == 47
    assert plan.retained_tokens == 40
    assert plan.retained_planned_tokens == 47
    assert counter.calls == ["mandatory", "optional", "summary"]


def test_hard_pressure_uses_one_shared_budget_and_verified_prefix_summary() -> None:
    mandatory = _group("mandatory", category=HistoryCategory.MANDATORY, sequence=1)
    exact = _group("exact", category=HistoryCategory.KEEP_EXACT, sequence=2)
    recent = _group("recent", category=HistoryCategory.RECENT, sequence=3)
    old = _group(
        "old",
        category=HistoryCategory.OPTIONAL,
        eligibility=SummaryEligibility.ELIGIBLE,
        sequence=4,
    )
    summary = _summary("summary", sequence=5, covered=(old,))
    plan = plan_history(
        (mandatory, exact, recent, old, summary),
        scope=SCOPE,
        frozen_watermark=10,
        counter=CountingCounter(
            {
                "mandatory": 10,
                "exact": 35,
                "recent": 25,
                "old": 30,
                "summary": 8,
            }
        ),
        b_work_tokens=100,
        summary_verifier=lambda group, scope: True,
    )

    assert plan.pre_trim_tokens == 100
    assert plan.pre_trim_pressure is PlanPressure.HARD
    assert plan.retained_tokens == 78
    assert plan.post_trim_pressure is PlanPressure.SOFT
    assert plan.retained_group_ids == (
        "mandatory",
        "exact",
        "recent",
        "summary",
    )
    assert plan.ready_for_dispatch
    covered = next(item for item in plan.decisions if item.group_id == "old")
    assert covered.reason is DecisionReason.DROPPED_COVERED_BY_SUMMARY
    assert covered.covered_by_summary_id == "summary"
    assert PlanReason.VERIFIED_SUMMARY_USED in plan.reasons


def test_overlapping_verified_summaries_are_never_both_selected() -> None:
    first = _group(
        "first",
        category=HistoryCategory.OPTIONAL,
        eligibility=SummaryEligibility.ELIGIBLE,
        sequence=1,
    )
    second = _group(
        "second",
        category=HistoryCategory.OPTIONAL,
        eligibility=SummaryEligibility.ELIGIBLE,
        sequence=2,
    )
    shorter = _summary("shorter", sequence=4, covered=(first,))
    longer = _summary("longer", sequence=3, covered=(first, second))
    plan = plan_history(
        (first, second, shorter, longer),
        scope=SCOPE,
        frozen_watermark=10,
        counter=CountingCounter({"first": 40, "second": 40, "shorter": 5, "longer": 5}),
        b_work_tokens=100,
        summary_verifier=lambda group, scope: True,
    )
    assert len({"shorter", "longer"} & set(plan.retained_group_ids)) == 1


def test_summary_requires_trusted_verifier_and_must_reduce_its_sources() -> None:
    source = _group(
        "old",
        category=HistoryCategory.OPTIONAL,
        eligibility=SummaryEligibility.ELIGIBLE,
        sequence=1,
    )
    summary = _summary("summary", sequence=2, covered=(source,))
    kwargs = dict(scope=SCOPE, frozen_watermark=10, b_work_tokens=100)
    unverified = plan_history(
        (source, summary), counter=CountingCounter({"old": 80, "summary": 10}), **kwargs
    )
    assert "summary" not in unverified.retained_group_ids
    no_savings = plan_history(
        (source, summary),
        counter=CountingCounter({"old": 80, "summary": 90}),
        summary_verifier=lambda group, scope: True,
        **kwargs,
    )
    assert "summary" not in no_savings.retained_group_ids
    assert no_savings.post_trim_pressure is PlanPressure.SOFT


def test_overlapping_summaries_choose_greatest_net_savings() -> None:
    first = _group(
        "first",
        category=HistoryCategory.OPTIONAL,
        eligibility=SummaryEligibility.ELIGIBLE,
        sequence=1,
    )
    second = _group(
        "second",
        category=HistoryCategory.OPTIONAL,
        eligibility=SummaryEligibility.ELIGIBLE,
        sequence=2,
    )
    short = _summary("short", sequence=4, covered=(first,))
    long = _summary("long", sequence=3, covered=(first, second))
    plan = plan_history(
        (first, second, short, long),
        scope=SCOPE,
        frozen_watermark=10,
        counter=CountingCounter({"first": 80, "second": 80, "short": 5, "long": 30}),
        b_work_tokens=100,
        summary_verifier=lambda group, scope: True,
    )
    assert plan.retained_group_ids == ("long",)
    assert plan.ready_for_dispatch and plan.retained_tokens == 30


def test_mandatory_group_overflow_uses_the_fixed_context_error() -> None:
    with pytest.raises(AgentHubError) as raised:
        plan_history(
            (_group("mandatory", category=HistoryCategory.MANDATORY),),
            scope=SCOPE,
            frozen_watermark=10,
            counter=CountingCounter({"mandatory": 101}),
            b_work_tokens=100,
        )

    assert raised.value.code == MANDATORY_OVERFLOW_CODE
    assert raised.value.message == "Mandatory context exceeds the available input budget."
    assert raised.value.status_code == 422


def test_work_limit_reports_incomplete_instead_of_claiming_full_measurement() -> None:
    groups = tuple(
        _group(
            f"group-{index}",
            category=HistoryCategory.OPTIONAL,
            sequence=index,
        )
        for index in range(1, 6)
    )
    counter = CountingCounter({group.group_id: 1 for group in groups})

    plan = plan_history(
        groups,
        scope=SCOPE,
        frozen_watermark=10,
        counter=counter,
        b_work_tokens=100,
        work_limit=HistoryWorkLimit(max_groups=2),
    )

    assert counter.calls == ["group-1", "group-2"]
    assert plan.measured_group_count == 2
    assert plan.unmeasured_group_count == 3
    assert not plan.measurement_complete
    assert plan.pre_trim_measurement_basis == "LOWER_BOUND"
    assert plan.unmeasured_group_ids == ("group-3", "group-4", "group-5")
    assert plan.pre_trim_tokens == 2
    assert not plan.ready_for_dispatch
    assert any(
        item.code is InsufficiencyCode.WORK_LIMIT_REACHED and item.blocking
        for item in plan.insufficiencies
    )


def test_keep_exact_omission_is_explicit_and_blocks_dispatch() -> None:
    exact = _group("exact", category=HistoryCategory.KEEP_EXACT, sequence=1)
    optional = _group("optional", category=HistoryCategory.OPTIONAL, sequence=2)
    plan = plan_history(
        (exact, optional),
        scope=SCOPE,
        frozen_watermark=10,
        counter=CountingCounter({"exact": 90, "optional": 1}),
        b_work_tokens=100,
        unaccounted_growth_tokens=0,
    )

    assert plan.decisions[0].retained
    assert not plan.ready_for_dispatch
    assert any(item.code is InsufficiencyCode.NO_ELIGIBLE_SOURCE for item in plan.insufficiencies)
    assert PlanReason.HARD_THRESHOLD_REACHED_BEFORE_TRIM in plan.reasons


def test_keep_exact_that_cannot_fit_is_reported_as_missing_not_failed_silently() -> None:
    exact = _group("exact", category=HistoryCategory.KEEP_EXACT)
    plan = plan_history(
        (exact,),
        scope=SCOPE,
        frozen_watermark=10,
        counter=CountingCounter({"exact": 101}),
        b_work_tokens=100,
    )

    assert not plan.ready_for_dispatch
    assert plan.dropped_group_ids == ("exact",)
    assert any(
        item.code is InsufficiencyCode.KEEP_EXACT_NOT_RETAINED and item.blocking
        for item in plan.insufficiencies
    )


def test_keep_exact_failure_reserves_unaccounted_growth() -> None:
    exact = _group("exact", category=HistoryCategory.KEEP_EXACT)
    plan = plan_history(
        (exact,),
        scope=SCOPE,
        frozen_watermark=10,
        counter=CountingCounter({"exact": 90}),
        b_work_tokens=100,
        unaccounted_growth_tokens=11,
    )

    assert not plan.ready_for_dispatch
    assert plan.dropped_group_ids == ("exact",)
    assert any(
        item.code is InsufficiencyCode.KEEP_EXACT_NOT_RETAINED for item in plan.insufficiencies
    )


def test_invalid_summary_coverage_cannot_replace_a_protected_group() -> None:
    first = _group(
        "first",
        category=HistoryCategory.OPTIONAL,
        eligibility=SummaryEligibility.ELIGIBLE,
        sequence=1,
    )
    second = _group(
        "second",
        category=HistoryCategory.OPTIONAL,
        eligibility=SummaryEligibility.ELIGIBLE,
        sequence=2,
    )
    later_only = _summary("later-only", sequence=3, covered=(second,))
    plan = plan_history(
        (first, second, later_only),
        scope=SCOPE,
        frozen_watermark=10,
        counter=CountingCounter({"first": 40, "second": 40, "later-only": 5}),
        b_work_tokens=100,
        summary_verifier=lambda group, scope: True,
    )

    summary_decision = next(item for item in plan.decisions if item.group_id == "later-only")
    assert summary_decision.reason is DecisionReason.DROPPED_SUMMARY_COVERAGE_INVALID
    assert plan.ready_for_dispatch
    assert any(
        item.code is InsufficiencyCode.VERIFIED_SUMMARY_COVERAGE_INVALID
        for item in plan.insufficiencies
    )


def test_frozen_scope_and_watermark_reject_concurrent_or_cross_scope_sources() -> None:
    concurrent = _group(
        "concurrent",
        category=HistoryCategory.OPTIONAL,
        watermark=11,
    )
    with pytest.raises(HistoryPlanContractError, match="frozen watermark"):
        plan_history(
            (concurrent,),
            scope=SCOPE,
            frozen_watermark=10,
            counter=CountingCounter({"concurrent": 1}),
            b_work_tokens=100,
        )

    other_scope = _group("other", category=HistoryCategory.OPTIONAL)
    object.__setattr__(
        other_scope,
        "scope",
        HistoryScope("workspace-2", "thread-1", "permission-7"),
    )
    with pytest.raises(HistoryPlanContractError, match="scope"):
        plan_history(
            (other_scope,),
            scope=SCOPE,
            frozen_watermark=10,
            counter=CountingCounter({"other": 1}),
            b_work_tokens=100,
        )


def test_atomic_exchange_cannot_be_split_across_source_groups() -> None:
    tool_call = _group("tool-call", category=HistoryCategory.MANDATORY)
    tool_result = HistorySourceGroup(
        group_id="tool-result",
        scope=SCOPE,
        revision="tool-result-revision",
        sequence=2,
        watermark=10,
        exchange_id=tool_call.exchange_id,
        original_ref="thread://tool-result",
        count_version=1,
        category=HistoryCategory.MANDATORY,
    )

    with pytest.raises(HistoryPlanContractError, match="atomic exchange"):
        plan_history(
            (tool_call, tool_result),
            scope=SCOPE,
            frozen_watermark=10,
            counter=CountingCounter({"tool-call": 1, "tool-result": 1}),
            b_work_tokens=100,
        )


def test_empty_history_is_complete_ready_and_zero_cost() -> None:
    plan = plan_history(
        (),
        scope=SCOPE,
        frozen_watermark=0,
        counter=CountingCounter({}),
        b_work_tokens=100,
    )

    assert plan.measurement_complete
    assert plan.ready_for_dispatch
    assert plan.pre_trim_tokens == 0
    assert plan.planned_tokens == 0
    assert plan.retained_group_ids == ()
