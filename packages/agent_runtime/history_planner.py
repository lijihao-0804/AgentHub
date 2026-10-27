"""Pure token-driven planning for M3 thread history.

The planner consumes immutable source-group metadata that a runtime adapter
has already read up to a frozen watermark.  It never inspects business text,
selects semantic categories, loads more history, or calls a model.  A caller
injects a counter whose identity and version must match every measured group.

Every source group is an admission unit.  Tool calls, their parallel results,
and paired evidence therefore stay together by construction.  Category and
summary eligibility are explicit caller-owned classifications.  Missing
category or summary-qualification evidence fails toward ``KEEP_EXACT``.

``pre_trim_tokens`` (N) counts complete non-summary candidate sources.
Published verified summaries are alternatives and are counted only if the
plan selects one.  ``unaccounted_growth_tokens`` (G) is a separate forecast
that is not present in those groups, so ``planned_tokens == N + G`` without
double counting.  Actual dispatch must still pass the existing
``ContextBudgetPolicy`` against the concrete model request.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from packages.agent_runtime.context_policy import THRESHOLD_PERCENTS
from packages.core.errors.exceptions import AgentHubError

PLANNER_VERSION = "history_planner_m3_v1"
SOFT_TRIGGER_PERCENT = 70
HARD_TRIGGER_PERCENT = 85
TARGET_PERCENT = 55
THRESHOLD_PERCENTS_M3 = MappingProxyType(
    {
        "soft": SOFT_TRIGGER_PERCENT,
        "hard": HARD_TRIGGER_PERCENT,
        "target": TARGET_PERCENT,
    }
)
MANDATORY_OVERFLOW_CODE = "AGENT_CONTEXT_BUDGET_EXCEEDED"
MANDATORY_OVERFLOW_MESSAGE = "Mandatory context exceeds the available input budget."

# Publication owns these M2 values.  Rejecting drift here keeps this planner
# from silently becoming a second source of threshold policy.
if THRESHOLD_PERCENTS != {
    "soft_trigger_percent": SOFT_TRIGGER_PERCENT,
    "hard_trigger_percent": HARD_TRIGGER_PERCENT,
    "target_percent": TARGET_PERCENT,
}:
    raise RuntimeError("M3 history planner thresholds drifted from context_policy")


class HistoryCategory(StrEnum):
    """Explicit admission category; no value is inferred from message text."""

    MANDATORY = "MANDATORY"
    KEEP_EXACT = "KEEP_EXACT"
    RECENT = "RECENT"
    VERIFIED_SUMMARY = "VERIFIED_SUMMARY"
    OPTIONAL = "OPTIONAL"


class SummaryEligibility(StrEnum):
    """Whether the source's publisher verified it as summarizable prefix text."""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNKNOWN = "UNKNOWN"


class PlanPressure(StrEnum):
    NONE = "NONE"
    SOFT = "SOFT"
    HARD = "HARD"


class PlanStatus(StrEnum):
    READY = "READY"
    INCOMPLETE = "INCOMPLETE"
    BLOCKED = "BLOCKED"


class DecisionReason(StrEnum):
    RETAINED_MANDATORY = "RETAINED_MANDATORY"
    RETAINED_KEEP_EXACT = "RETAINED_KEEP_EXACT"
    RETAINED_RECENT = "RETAINED_RECENT"
    RETAINED_VERIFIED_SUMMARY = "RETAINED_VERIFIED_SUMMARY"
    RETAINED_OPTIONAL = "RETAINED_OPTIONAL"
    DROPPED_SHARED_BUDGET = "DROPPED_SHARED_BUDGET"
    DROPPED_RECENT_SUFFIX_GAP = "DROPPED_RECENT_SUFFIX_GAP"
    DROPPED_COVERED_BY_SUMMARY = "DROPPED_COVERED_BY_SUMMARY"
    DROPPED_SUMMARY_NOT_NEEDED = "DROPPED_SUMMARY_NOT_NEEDED"
    DROPPED_SUMMARY_COVERAGE_INVALID = "DROPPED_SUMMARY_COVERAGE_INVALID"


class EligibilityReason(StrEnum):
    EXPLICIT_CATEGORY = "EXPLICIT_CATEGORY"
    UNKNOWN_CATEGORY_KEEP_EXACT = "UNKNOWN_CATEGORY_KEEP_EXACT"
    UNKNOWN_SUMMARY_ELIGIBILITY_KEEP_EXACT = "UNKNOWN_SUMMARY_ELIGIBILITY_KEEP_EXACT"
    PUBLISHER_MARKED_SUMMARY_ELIGIBLE = "PUBLISHER_MARKED_SUMMARY_ELIGIBLE"
    PUBLISHER_MARKED_SUMMARY_INELIGIBLE = "PUBLISHER_MARKED_SUMMARY_INELIGIBLE"
    VERIFIED_SUMMARY = "VERIFIED_SUMMARY"


class PlanReason(StrEnum):
    NO_PRESSURE = "NO_PRESSURE"
    SOFT_THRESHOLD_REACHED_BEFORE_TRIM = "SOFT_THRESHOLD_REACHED_BEFORE_TRIM"
    HARD_THRESHOLD_REACHED_BEFORE_TRIM = "HARD_THRESHOLD_REACHED_BEFORE_TRIM"
    TARGET_MET_AFTER_PLANNING = "TARGET_MET_AFTER_PLANNING"
    SHARED_BUDGET_TRIM_REQUIRED = "SHARED_BUDGET_TRIM_REQUIRED"
    VERIFIED_SUMMARY_USED = "VERIFIED_SUMMARY_USED"
    WORK_LIMIT_REACHED = "WORK_LIMIT_REACHED"
    KEEP_EXACT_OMITTED = "KEEP_EXACT_OMITTED"
    RECENT_GROUP_OMITTED = "RECENT_GROUP_OMITTED"
    VERIFIED_SUMMARY_COVERAGE_INVALID = "VERIFIED_SUMMARY_COVERAGE_INVALID"
    HARD_LIMIT_NOT_SATISFIED = "HARD_LIMIT_NOT_SATISFIED"


class InsufficiencyCode(StrEnum):
    WORK_LIMIT_REACHED = "WORK_LIMIT_REACHED"
    NO_ELIGIBLE_SOURCE = "NO_ELIGIBLE_SOURCE"
    NO_VERIFIED_SUMMARY = "NO_VERIFIED_SUMMARY"
    KEEP_EXACT_NOT_RETAINED = "KEEP_EXACT_NOT_RETAINED"
    RECENT_SOURCE_NOT_RETAINED = "RECENT_SOURCE_NOT_RETAINED"
    VERIFIED_SUMMARY_NOT_RETAINED = "VERIFIED_SUMMARY_NOT_RETAINED"
    VERIFIED_SUMMARY_COVERAGE_INVALID = "VERIFIED_SUMMARY_COVERAGE_INVALID"
    POST_TRIM_PLANNED_AT_OR_ABOVE_HARD_LIMIT = "POST_TRIM_PLANNED_AT_OR_ABOVE_HARD_LIMIT"


class HistoryPlanContractError(ValueError):
    """The caller supplied internally inconsistent source-group metadata."""


@dataclass(frozen=True, slots=True)
class HistoryScope:
    """Tenant and thread boundary for every group in one plan."""

    workspace_id: str
    thread_id: str
    permission_snapshot: str

    def __post_init__(self) -> None:
        _require_text(self.workspace_id, "workspace_id")
        _require_text(self.thread_id, "thread_id")
        _require_text(self.permission_snapshot, "permission_snapshot")


@dataclass(frozen=True, slots=True)
class SummaryCoverage:
    """One exact source revision covered by a verified summary."""

    source_group_id: str
    source_revision: str

    def __post_init__(self) -> None:
        _require_text(self.source_group_id, "source_group_id")
        _require_text(self.source_revision, "source_revision")


@dataclass(frozen=True, slots=True)
class HistorySourceGroup:
    """An immutable, complete admission unit.

    ``group_id`` is a stable logical identity.  ``exchange_id`` is the stable
    logical exchange identity and must not be a provider-generated tool-call
    ID.  If a tool call and its results, or an evidence record and its paired
    citation, need to enter the model, they belong to the same group and can
    only be retained or dropped together.
    """

    group_id: str
    scope: HistoryScope
    revision: str
    sequence: int
    watermark: int
    exchange_id: str
    original_ref: str
    count_version: int
    category: HistoryCategory | str | None = None
    summary_eligibility: SummaryEligibility | str = SummaryEligibility.UNKNOWN
    summary_eligibility_reason: str = "UNSPECIFIED"
    coverage: tuple[SummaryCoverage, ...] = ()
    verified: bool = False
    verification_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.group_id, "group_id")
        if not isinstance(self.scope, HistoryScope):
            raise TypeError("scope must be HistoryScope")
        _require_text(self.revision, "revision")
        _require_non_negative_int(self.sequence, "sequence")
        _require_non_negative_int(self.watermark, "watermark")
        _require_text(self.exchange_id, "exchange_id")
        _require_text(self.original_ref, "original_ref")
        _require_positive_int(self.count_version, "count_version")
        if self.category is not None:
            try:
                category = HistoryCategory(self.category)
            except (TypeError, ValueError):
                raise ValueError("category is invalid") from None
            object.__setattr__(self, "category", category)
        try:
            eligibility = SummaryEligibility(self.summary_eligibility)
        except (TypeError, ValueError):
            raise ValueError("summary_eligibility is invalid") from None
        object.__setattr__(self, "summary_eligibility", eligibility)
        _require_text(self.summary_eligibility_reason, "summary_eligibility_reason")
        coverage = tuple(self.coverage)
        if any(not isinstance(item, SummaryCoverage) for item in coverage):
            raise TypeError("coverage must contain SummaryCoverage values")
        if len({(item.source_group_id, item.source_revision) for item in coverage}) != len(
            coverage
        ):
            raise ValueError("coverage contains duplicate source revisions")
        object.__setattr__(self, "coverage", coverage)
        if not isinstance(self.verified, bool):
            raise TypeError("verified must be a bool")
        if self.verification_ref is not None:
            _require_text(self.verification_ref, "verification_ref")
        if self.category is HistoryCategory.VERIFIED_SUMMARY:
            if not self.verified or self.verification_ref is None or not coverage:
                raise ValueError("VERIFIED_SUMMARY requires a verification_ref and coverage")
        elif self.verified or self.verification_ref is not None or coverage:
            raise ValueError("only VERIFIED_SUMMARY groups may carry verification or coverage")
        if (
            self.summary_eligibility is SummaryEligibility.ELIGIBLE
            and self.category is not HistoryCategory.OPTIONAL
        ):
            raise ValueError("only explicit OPTIONAL groups may be summary-eligible")


@runtime_checkable
class HistoryTokenCounter(Protocol):
    """Injected deterministic whole-group counter."""

    counter_id: str
    counter_version: int

    def count(self, group: HistorySourceGroup) -> int:
        """Return the token count of the complete group."""


@dataclass(frozen=True, slots=True)
class HistoryWorkLimit:
    """Maximum number of groups whose tokens are measured in one plan."""

    max_groups: int

    def __post_init__(self) -> None:
        _require_positive_int(self.max_groups, "max_groups")


@dataclass(frozen=True, slots=True)
class HistoryThresholds:
    """Frozen M2 integer percentages and their exact B_work token cutoffs."""

    b_work_tokens: int
    soft_percent: int
    hard_percent: int
    target_percent: int
    soft_tokens: int
    hard_tokens: int
    target_tokens: int


@dataclass(frozen=True, slots=True)
class SourceGroupDecision:
    """The whole-group result for one measured source."""

    group_id: str
    revision: str
    sequence: int
    exchange_id: str
    category: HistoryCategory
    summary_eligibility: SummaryEligibility
    summary_eligibility_reason: str
    eligibility_reason: EligibilityReason
    tokens: int
    retained: bool
    reason: DecisionReason
    covered_by_summary_id: str | None = None


@dataclass(frozen=True, slots=True)
class Insufficiency:
    """A stable, text-free explanation that the plan is incomplete."""

    code: InsufficiencyCode
    blocking: bool
    message: str


@dataclass(frozen=True, slots=True)
class HistoryPlan:
    """Pure plan plus the counters needed to reconcile it with runtime admission."""

    plan_version: str
    scope: HistoryScope
    frozen_watermark: int
    counter_id: str
    counter_version: int
    work_limit: int
    input_group_count: int
    measured_group_count: int
    unmeasured_group_count: int
    measurement_complete: bool
    pre_trim_measurement_basis: str
    unmeasured_group_ids: tuple[str, ...]
    thresholds: HistoryThresholds
    pre_trim_tokens: int
    unaccounted_growth_tokens: int
    planned_tokens: int
    retained_tokens: int
    retained_planned_tokens: int
    pre_trim_pressure: PlanPressure
    post_trim_pressure: PlanPressure
    retained_group_ids: tuple[str, ...]
    dropped_group_ids: tuple[str, ...]
    decisions: tuple[SourceGroupDecision, ...]
    reasons: tuple[PlanReason, ...]
    insufficiencies: tuple[Insufficiency, ...]
    ready_for_dispatch: bool

    @property
    def status(self) -> PlanStatus:
        if any(item.blocking for item in self.insufficiencies):
            return PlanStatus.INCOMPLETE if not self.measurement_complete else PlanStatus.BLOCKED
        return PlanStatus.READY


@dataclass(frozen=True, slots=True)
class _MeasuredGroup:
    group: HistorySourceGroup
    category: HistoryCategory
    eligibility_reason: EligibilityReason
    tokens: int


def plan_history(
    groups: Sequence[HistorySourceGroup],
    *,
    scope: HistoryScope,
    frozen_watermark: int,
    counter: HistoryTokenCounter,
    b_work_tokens: int,
    unaccounted_growth_tokens: int = 0,
    work_limit: HistoryWorkLimit | None = None,
    summary_verifier: Callable[[HistorySourceGroup, HistoryScope], bool] | None = None,
    expected_counter_id: str | None = None,
    expected_counter_version: int | None = None,
) -> HistoryPlan:
    """Return a deterministic all-or-nothing source-group plan.

    The caller must freeze pagination before constructing ``groups``.  Groups
    beyond ``frozen_watermark``, across another scope, split across multiple
    ``exchange_id`` values, or measured by another counter version are rejected
    as contract errors.  A group that does not fit the shared budget is dropped
    whole; no source can be partially selected or projected here.
    """

    if not isinstance(scope, HistoryScope):
        raise TypeError("scope must be HistoryScope")
    _require_non_negative_int(frozen_watermark, "frozen_watermark")
    _require_positive_int(b_work_tokens, "b_work_tokens")
    _require_non_negative_int(unaccounted_growth_tokens, "unaccounted_growth_tokens")
    limit = work_limit or HistoryWorkLimit(max_groups=256)
    _validate_counter(counter)
    if expected_counter_id is not None and counter.counter_id != expected_counter_id:
        raise HistoryPlanContractError("history counter differs from the frozen policy")
    if expected_counter_version is not None and counter.counter_version != expected_counter_version:
        raise HistoryPlanContractError("history counter version differs from the frozen policy")
    ordered = _validate_and_order_groups(
        groups,
        scope=scope,
        frozen_watermark=frozen_watermark,
        counter_version=counter.counter_version,
    )
    measured_inputs = ordered[: limit.max_groups]
    measured = tuple(_measure_group(group, counter) for group in measured_inputs)
    measurement_complete = len(ordered) <= limit.max_groups
    unmeasured_count = max(len(ordered) - len(measured), 0)

    decisions: dict[str, SourceGroupDecision] = {}
    token_counts = {item.group.group_id: item.tokens for item in measured}
    source_groups = tuple(
        item for item in measured if item.category is not HistoryCategory.VERIFIED_SUMMARY
    )
    summary_groups = tuple(
        item for item in measured if item.category is HistoryCategory.VERIFIED_SUMMARY
    )
    pre_trim_tokens = sum(item.tokens for item in source_groups)
    planned_tokens = pre_trim_tokens + unaccounted_growth_tokens
    thresholds = _thresholds(b_work_tokens)
    pre_trim_pressure = _pressure(planned_tokens, thresholds)

    mandatory = tuple(item for item in source_groups if item.category is HistoryCategory.MANDATORY)
    mandatory_total = sum(item.tokens for item in mandatory)
    if mandatory_total > b_work_tokens:
        raise AgentHubError(
            MANDATORY_OVERFLOW_CODE,
            MANDATORY_OVERFLOW_MESSAGE,
            422,
        )

    selected_ids: set[str] = set()
    remaining = b_work_tokens
    reason_codes: list[PlanReason] = []
    insufficiencies: list[Insufficiency] = []

    for item in mandatory:
        selected_ids.add(item.group.group_id)
        remaining -= item.tokens
        decisions[item.group.group_id] = _source_decision(
            item,
            retained=True,
            reason=DecisionReason.RETAINED_MANDATORY,
        )

    # G is absent from every source group, but it still competes for the same
    # working window.  Reserve it before source selection so a post-trim
    # pressure result cannot look healthy by ignoring expected growth.
    if unaccounted_growth_tokens > remaining:
        raise AgentHubError(
            "AGENT_HISTORY_GROWTH_BUDGET_EXCEEDED",
            "Anticipated context growth exceeds the remaining input budget.",
            422,
        )
    remaining -= unaccounted_growth_tokens

    keep_exact = tuple(
        item for item in source_groups if item.category is HistoryCategory.KEEP_EXACT
    )
    for item in _newest_first(keep_exact):
        if item.tokens <= remaining:
            selected_ids.add(item.group.group_id)
            remaining -= item.tokens
            decisions[item.group.group_id] = _source_decision(
                item,
                retained=True,
                reason=DecisionReason.RETAINED_KEEP_EXACT,
            )
        else:
            decisions[item.group.group_id] = _source_decision(
                item,
                retained=False,
                reason=DecisionReason.DROPPED_SHARED_BUDGET,
            )

    recent = tuple(item for item in source_groups if item.category is HistoryCategory.RECENT)
    recent_suffix_open = True
    for item in _newest_first(recent):
        if recent_suffix_open and item.tokens <= remaining:
            selected_ids.add(item.group.group_id)
            remaining -= item.tokens
            decisions[item.group.group_id] = _source_decision(
                item,
                retained=True,
                reason=DecisionReason.RETAINED_RECENT,
            )
        else:
            recent_suffix_open = False
            decisions[item.group.group_id] = _source_decision(
                item,
                retained=False,
                reason=DecisionReason.DROPPED_RECENT_SUFFIX_GAP,
            )

    eligible = tuple(
        item
        for item in source_groups
        if item.category is HistoryCategory.OPTIONAL
        and item.group.summary_eligibility is SummaryEligibility.ELIGIBLE
    )
    coverage_validation = _coverage_validation(
        summary_groups,
        source_groups=source_groups,
        eligible=eligible,
    )
    for item in summary_groups:
        if summary_verifier is None or summary_verifier(item.group, scope) is not True:
            coverage_validation[item.group.group_id] = "summary publication is not verified"
    covered_ids: set[str] = set()

    if pre_trim_pressure is not PlanPressure.NONE and eligible:
        summaries = tuple(
            sorted(
                summary_groups,
                key=lambda item: (
                    -(
                        sum(
                            token_counts.get(coverage.source_group_id, 0)
                            for coverage in item.group.coverage
                        )
                        - item.tokens
                    ),
                    -item.group.sequence,
                    item.group.group_id,
                ),
            )
        )
        for item in summaries:
            invalid_reason = coverage_validation.get(item.group.group_id)
            covered = {coverage.source_group_id for coverage in item.group.coverage}
            if invalid_reason is not None:
                decisions[item.group.group_id] = _source_decision(
                    item,
                    retained=False,
                    reason=DecisionReason.DROPPED_SUMMARY_COVERAGE_INVALID,
                )
                continue
            if covered & covered_ids:
                decisions[item.group.group_id] = _source_decision(
                    item,
                    retained=False,
                    reason=DecisionReason.DROPPED_SUMMARY_NOT_NEEDED,
                )
                continue
            covered_cost = sum(token_counts[source_id] for source_id in covered)
            if item.tokens >= covered_cost:
                decisions[item.group.group_id] = _source_decision(
                    item,
                    retained=False,
                    reason=DecisionReason.DROPPED_SUMMARY_NOT_NEEDED,
                )
                continue
            if item.tokens <= remaining:
                selected_ids.add(item.group.group_id)
                remaining -= item.tokens
                covered_ids.update(covered)
                decisions[item.group.group_id] = _source_decision(
                    item,
                    retained=True,
                    reason=DecisionReason.RETAINED_VERIFIED_SUMMARY,
                )
            else:
                decisions[item.group.group_id] = _source_decision(
                    item,
                    retained=False,
                    reason=DecisionReason.DROPPED_SHARED_BUDGET,
                )
    else:
        for item in summary_groups:
            decisions[item.group.group_id] = _source_decision(
                item,
                retained=False,
                reason=DecisionReason.DROPPED_SUMMARY_NOT_NEEDED,
            )

    optional = tuple(item for item in source_groups if item.category is HistoryCategory.OPTIONAL)
    for item in _newest_first(optional):
        if item.group.group_id in covered_ids:
            summary_id = _covering_summary_id(item, summary_groups, decisions)
            decisions[item.group.group_id] = _source_decision(
                item,
                retained=False,
                reason=DecisionReason.DROPPED_COVERED_BY_SUMMARY,
                covered_by_summary_id=summary_id,
            )
        elif item.tokens <= remaining:
            selected_ids.add(item.group.group_id)
            remaining -= item.tokens
            decisions[item.group.group_id] = _source_decision(
                item,
                retained=True,
                reason=DecisionReason.RETAINED_OPTIONAL,
            )
        else:
            decisions[item.group.group_id] = _source_decision(
                item,
                retained=False,
                reason=DecisionReason.DROPPED_SHARED_BUDGET,
            )

    retained_tokens = sum(token_counts[group_id] for group_id in selected_ids)
    retained_planned_tokens = retained_tokens + unaccounted_growth_tokens
    post_trim_pressure = _pressure(retained_planned_tokens, thresholds)

    if not measurement_complete:
        insufficiencies.append(
            _insufficiency(
                InsufficiencyCode.WORK_LIMIT_REACHED,
                blocking=True,
                message=(
                    f"Only {len(measured)} of {len(ordered)} groups were measured; "
                    "the remaining history must not be reported as measured."
                ),
            )
        )
        reason_codes.append(PlanReason.WORK_LIMIT_REACHED)

    dropped_keep_exact = any(
        item.category is HistoryCategory.KEEP_EXACT and item.group.group_id not in selected_ids
        for item in source_groups
    )
    if dropped_keep_exact:
        insufficiencies.append(
            _insufficiency(
                InsufficiencyCode.KEEP_EXACT_NOT_RETAINED,
                blocking=True,
                message="At least one KEEP_EXACT source group did not fit the shared budget.",
            )
        )
        reason_codes.append(PlanReason.KEEP_EXACT_OMITTED)

    dropped_recent = any(
        item.category is HistoryCategory.RECENT and item.group.group_id not in selected_ids
        for item in source_groups
    )
    if dropped_recent:
        insufficiencies.append(
            _insufficiency(
                InsufficiencyCode.RECENT_SOURCE_NOT_RETAINED,
                blocking=True,
                message="The complete recent-source suffix could not be retained.",
            )
        )
        reason_codes.append(PlanReason.RECENT_GROUP_OMITTED)

    invalid_summaries = tuple(
        item for item in summary_groups if item.group.group_id in coverage_validation
    )
    if invalid_summaries:
        insufficiencies.append(
            _insufficiency(
                InsufficiencyCode.VERIFIED_SUMMARY_COVERAGE_INVALID,
                blocking=False,
                message=(
                    "A verified summary did not bind the complete oldest eligible source prefix."
                ),
            )
        )
        reason_codes.append(PlanReason.VERIFIED_SUMMARY_COVERAGE_INVALID)

    summary_selected = any(
        item.category is HistoryCategory.VERIFIED_SUMMARY and item.group.group_id in selected_ids
        for item in summary_groups
    )
    if pre_trim_pressure is not PlanPressure.NONE:
        if not eligible:
            insufficiencies.append(
                _insufficiency(
                    InsufficiencyCode.NO_ELIGIBLE_SOURCE,
                    blocking=False,
                    message="No source group has explicit summary eligibility.",
                )
            )
        elif not summary_groups:
            insufficiencies.append(
                _insufficiency(
                    InsufficiencyCode.NO_VERIFIED_SUMMARY,
                    blocking=False,
                    message="No published verified summary was supplied.",
                )
            )
        elif not summary_selected and invalid_summaries:
            pass
        elif not summary_selected:
            insufficiencies.append(
                _insufficiency(
                    InsufficiencyCode.VERIFIED_SUMMARY_NOT_RETAINED,
                    blocking=False,
                    message="No supplied verified summary fit the shared budget.",
                )
            )

    if post_trim_pressure is PlanPressure.HARD:
        insufficiencies.append(
            _insufficiency(
                InsufficiencyCode.POST_TRIM_PLANNED_AT_OR_ABOVE_HARD_LIMIT,
                blocking=True,
                message=("Post-trim planned input remains at or above the hard threshold."),
            )
        )
        reason_codes.append(PlanReason.HARD_LIMIT_NOT_SATISFIED)

    if pre_trim_pressure is PlanPressure.NONE:
        reason_codes.append(PlanReason.NO_PRESSURE)
    elif pre_trim_pressure is PlanPressure.SOFT:
        reason_codes.append(PlanReason.SOFT_THRESHOLD_REACHED_BEFORE_TRIM)
    else:
        reason_codes.append(PlanReason.HARD_THRESHOLD_REACHED_BEFORE_TRIM)
    if retained_planned_tokens <= thresholds.target_tokens:
        reason_codes.append(PlanReason.TARGET_MET_AFTER_PLANNING)
    if retained_tokens < pre_trim_tokens:
        reason_codes.append(PlanReason.SHARED_BUDGET_TRIM_REQUIRED)
    if summary_selected:
        reason_codes.append(PlanReason.VERIFIED_SUMMARY_USED)

    retained_group_ids = tuple(
        item.group.group_id for item in measured if item.group.group_id in selected_ids
    )
    dropped_group_ids = tuple(
        item.group.group_id for item in measured if item.group.group_id not in selected_ids
    )
    ordered_decisions = tuple(decisions[item.group.group_id] for item in measured)
    ready_for_dispatch = not any(item.blocking for item in insufficiencies)
    return HistoryPlan(
        plan_version=PLANNER_VERSION,
        scope=scope,
        frozen_watermark=frozen_watermark,
        counter_id=counter.counter_id,
        counter_version=counter.counter_version,
        work_limit=limit.max_groups,
        input_group_count=len(ordered),
        measured_group_count=len(measured),
        unmeasured_group_count=unmeasured_count,
        measurement_complete=measurement_complete,
        pre_trim_measurement_basis="EXACT" if measurement_complete else "LOWER_BOUND",
        unmeasured_group_ids=tuple(item.group_id for item in ordered[limit.max_groups :]),
        thresholds=thresholds,
        pre_trim_tokens=pre_trim_tokens,
        unaccounted_growth_tokens=unaccounted_growth_tokens,
        planned_tokens=planned_tokens,
        retained_tokens=retained_tokens,
        retained_planned_tokens=retained_planned_tokens,
        pre_trim_pressure=pre_trim_pressure,
        post_trim_pressure=post_trim_pressure,
        retained_group_ids=retained_group_ids,
        dropped_group_ids=dropped_group_ids,
        decisions=ordered_decisions,
        reasons=_deduplicate_reasons(reason_codes),
        insufficiencies=tuple(insufficiencies),
        ready_for_dispatch=ready_for_dispatch,
    )


def _validate_and_order_groups(
    groups: Sequence[HistorySourceGroup],
    *,
    scope: HistoryScope,
    frozen_watermark: int,
    counter_version: int,
) -> tuple[HistorySourceGroup, ...]:
    if isinstance(groups, (str, bytes)) or not isinstance(groups, Sequence):
        raise TypeError("groups must be a sequence of HistorySourceGroup values")
    values = tuple(groups)
    if any(not isinstance(group, HistorySourceGroup) for group in values):
        raise TypeError("groups must contain HistorySourceGroup values")
    identities: dict[str, tuple[str, str]] = {}
    exchange_owners: dict[str, str] = {}
    for group in values:
        if group.scope != scope:
            raise HistoryPlanContractError("source scope differs from the frozen plan scope")
        if group.watermark != frozen_watermark or group.sequence >= frozen_watermark:
            raise HistoryPlanContractError("source group is newer than the frozen watermark")
        if group.count_version != counter_version:
            raise HistoryPlanContractError("source group count version differs from its counter")
        identity = (group.revision, group.exchange_id)
        previous = identities.get(group.group_id)
        if previous is not None:
            raise HistoryPlanContractError("duplicate source group identity")
        identities[group.group_id] = identity
        exchange_owner = exchange_owners.get(group.exchange_id)
        if exchange_owner is not None:
            raise HistoryPlanContractError("one atomic exchange was split across source groups")
        exchange_owners[group.exchange_id] = group.group_id
    return tuple(sorted(values, key=lambda item: (item.sequence, item.group_id)))


def _measure_group(
    group: HistorySourceGroup,
    counter: HistoryTokenCounter,
) -> _MeasuredGroup:
    category, eligibility_reason = _effective_category(group)
    try:
        tokens = counter.count(group)
    except Exception:
        raise HistoryPlanContractError("injected history counter failed") from None
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise HistoryPlanContractError(
            "injected history counter must return a non-negative integer"
        )
    return _MeasuredGroup(
        group=group,
        category=category,
        eligibility_reason=eligibility_reason,
        tokens=tokens,
    )


def _effective_category(
    group: HistorySourceGroup,
) -> tuple[HistoryCategory, EligibilityReason]:
    category = group.category
    if category is None:
        return (
            HistoryCategory.KEEP_EXACT,
            EligibilityReason.UNKNOWN_CATEGORY_KEEP_EXACT,
        )
    if (
        category is HistoryCategory.OPTIONAL
        and group.summary_eligibility is SummaryEligibility.UNKNOWN
    ):
        return (
            HistoryCategory.KEEP_EXACT,
            EligibilityReason.UNKNOWN_SUMMARY_ELIGIBILITY_KEEP_EXACT,
        )
    if group.summary_eligibility is SummaryEligibility.ELIGIBLE:
        return category, EligibilityReason.PUBLISHER_MARKED_SUMMARY_ELIGIBLE
    if group.summary_eligibility is SummaryEligibility.INELIGIBLE:
        return category, EligibilityReason.PUBLISHER_MARKED_SUMMARY_INELIGIBLE
    if category is HistoryCategory.VERIFIED_SUMMARY:
        return category, EligibilityReason.VERIFIED_SUMMARY
    return category, EligibilityReason.EXPLICIT_CATEGORY


def _coverage_validation(
    summaries: tuple[_MeasuredGroup, ...],
    *,
    source_groups: tuple[_MeasuredGroup, ...],
    eligible: tuple[_MeasuredGroup, ...],
) -> dict[str, str]:
    result: dict[str, str] = {}
    source_revisions = {item.group.group_id: item.group.revision for item in source_groups}
    eligible_ids = {item.group.group_id for item in eligible}
    eligible_order = tuple(item.group.group_id for item in eligible)
    for summary in summaries:
        coverage_ids = tuple(item.source_group_id for item in summary.group.coverage)
        coverage_revisions = tuple(item.source_revision for item in summary.group.coverage)
        if any(
            source_revisions.get(source_id) != revision
            for source_id, revision in zip(coverage_ids, coverage_revisions, strict=True)
        ):
            result[summary.group.group_id] = "coverage source revision is unavailable"
            continue
        if any(source_id not in eligible_ids for source_id in coverage_ids):
            result[summary.group.group_id] = "coverage includes a non-eligible source"
            continue
        expected_prefix = eligible_order[: len(coverage_ids)]
        if coverage_ids != expected_prefix:
            result[summary.group.group_id] = "coverage is not the oldest complete eligible prefix"
    return result


def _covering_summary_id(
    item: _MeasuredGroup,
    summaries: tuple[_MeasuredGroup, ...],
    decisions: dict[str, SourceGroupDecision],
) -> str | None:
    for summary in summaries:
        decision = decisions.get(summary.group.group_id)
        if decision is None or not decision.retained:
            continue
        if any(
            coverage.source_group_id == item.group.group_id
            and coverage.source_revision == item.group.revision
            for coverage in summary.group.coverage
        ):
            return summary.group.group_id
    return None


def _source_decision(
    item: _MeasuredGroup,
    *,
    retained: bool,
    reason: DecisionReason,
    covered_by_summary_id: str | None = None,
) -> SourceGroupDecision:
    return SourceGroupDecision(
        group_id=item.group.group_id,
        revision=item.group.revision,
        sequence=item.group.sequence,
        exchange_id=item.group.exchange_id,
        category=item.category,
        summary_eligibility=item.group.summary_eligibility,
        summary_eligibility_reason=item.group.summary_eligibility_reason,
        eligibility_reason=item.eligibility_reason,
        tokens=item.tokens,
        retained=retained,
        reason=reason,
        covered_by_summary_id=covered_by_summary_id,
    )


def _thresholds(b_work_tokens: int) -> HistoryThresholds:
    return HistoryThresholds(
        b_work_tokens=b_work_tokens,
        soft_percent=SOFT_TRIGGER_PERCENT,
        hard_percent=HARD_TRIGGER_PERCENT,
        target_percent=TARGET_PERCENT,
        soft_tokens=(SOFT_TRIGGER_PERCENT * b_work_tokens + 99) // 100,
        hard_tokens=(HARD_TRIGGER_PERCENT * b_work_tokens + 99) // 100,
        target_tokens=(TARGET_PERCENT * b_work_tokens) // 100,
    )


def _pressure(tokens: int, thresholds: HistoryThresholds) -> PlanPressure:
    if tokens >= thresholds.hard_tokens:
        return PlanPressure.HARD
    if tokens >= thresholds.soft_tokens:
        return PlanPressure.SOFT
    return PlanPressure.NONE


def _newest_first(groups: tuple[_MeasuredGroup, ...]) -> tuple[_MeasuredGroup, ...]:
    return tuple(
        sorted(
            groups,
            key=lambda item: (item.group.sequence, item.group.group_id),
            reverse=True,
        )
    )


def _insufficiency(
    code: InsufficiencyCode,
    *,
    blocking: bool,
    message: str,
) -> Insufficiency:
    return Insufficiency(code=code, blocking=blocking, message=message)


def _deduplicate_reasons(values: Sequence[PlanReason]) -> tuple[PlanReason, ...]:
    result: list[PlanReason] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


def _validate_counter(counter: HistoryTokenCounter) -> None:
    if not isinstance(counter, HistoryTokenCounter):
        raise TypeError("counter must implement counter_id, counter_version, and count")
    _require_text(counter.counter_id, "counter_id")
    _require_positive_int(counter.counter_version, "counter_version")


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


__all__ = [
    "DecisionReason",
    "EligibilityReason",
    "HARD_TRIGGER_PERCENT",
    "HistoryCategory",
    "HistoryPlan",
    "HistoryPlanContractError",
    "HistoryScope",
    "HistorySourceGroup",
    "HistoryThresholds",
    "HistoryTokenCounter",
    "HistoryWorkLimit",
    "Insufficiency",
    "InsufficiencyCode",
    "MANDATORY_OVERFLOW_CODE",
    "MANDATORY_OVERFLOW_MESSAGE",
    "PLANNER_VERSION",
    "PlanPressure",
    "PlanReason",
    "PlanStatus",
    "SOFT_TRIGGER_PERCENT",
    "SourceGroupDecision",
    "SummaryCoverage",
    "SummaryEligibility",
    "TARGET_PERCENT",
    "THRESHOLD_PERCENTS_M3",
    "plan_history",
]
