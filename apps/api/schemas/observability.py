from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ObservabilityWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: datetime = Field(alias="from")
    to: datetime


class RateMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    numerator: int
    denominator: int
    rate: float | None


class FinishedRunsMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    succeeded: int
    failed: int
    cancelled: int
    needs_attention: int
    denominator: int


class LatencyMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p50_ms: float | None
    p95_ms: float | None
    avg_ms: float | None = None
    sample_count: int


class UsageMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    avg_tokens_per_run: float | None
    avg_input_tokens: float | None
    avg_output_tokens: float | None
    avg_cached_tokens: float | None
    total_tokens: int | None
    known_usage_count: int
    unknown_usage_count: int


class CostCurrencyMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str
    total_cost: Decimal | None
    avg_cost_per_run: Decimal | None
    cost_per_successful_run: Decimal | None
    successful_cost_denominator: int
    known_cost_count: int
    estimated_count: int
    exact_count: int


class CostMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str | None
    total_cost: Decimal | None
    avg_cost_per_run: Decimal | None
    cost_per_successful_run: Decimal | None
    successful_cost_denominator: int
    estimated_count: int
    exact_count: int
    mixed_currency: bool
    currencies: list[CostCurrencyMetric]


class CurrentStateMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    running_count: int
    waiting_approval_count: int
    cancel_requested_count: int
    needs_attention_count: int
    unknown_outcome_action_count: int


class ApprovalWaitMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p50_ms: float | None
    p95_ms: float | None
    sample_count: int


class ApprovalMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_total: int
    pending: int
    approved: int
    denied: int
    expired: int
    cancelled: int
    execution_not_started: int
    claimed: int
    succeeded: int
    failed: int
    unknown_outcome: int
    wait_latency: ApprovalWaitMetric


class ObservabilitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: ObservabilityWindow
    finished_runs: FinishedRunsMetric
    success_rate: RateMetric
    failure_rate: RateMetric
    cancelled_rate: RateMetric
    needs_attention_rate: RateMetric
    latency: LatencyMetric
    usage: UsageMetric
    cost: CostMetric
    current: CurrentStateMetric
    approvals: ApprovalMetric


class FailureCodeMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_code: str
    count: int


class FailureCategoryMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_category: str
    count: int
    percentage: float | None
    top_failure_codes: list[FailureCodeMetric]


class FailureRunItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    agent_version_id: UUID
    agent_version_number: int
    status: str
    failure_category: str
    failure_code: str
    started_at: datetime
    completed_at: datetime | None
    duration_ms: float | None
    tool_call_count: int
    approval_id: UUID | None
    logical_action_id: str | None
    tool_identity: str | None
    execution_status: str | None
    action_failure_code: str | None


class FailureAnalyticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: ObservabilityWindow
    total_failure_runs: int
    categories: list[FailureCategoryMetric]
    items: list[FailureRunItem]


class AgentVersionMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_version_id: UUID
    version_number: int
    run_count: int
    success_count: int
    failed_count: int
    needs_attention_count: int
    p95_latency_ms: float | None
    avg_tokens: float | None
    cost_by_currency: list[dict[str, Any]]


class AgentVersionBreakdownResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: ObservabilityWindow
    items: list[AgentVersionMetric]


class TimeseriesItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket: datetime
    runs: int
    succeeded: int
    failed: int
    needs_attention: int
    tokens: int | None
    cost_by_currency: list[dict[str, Any]]


class TimeseriesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: ObservabilityWindow
    bucket: str
    items: list[TimeseriesItem]


__all__ = [
    "AgentVersionBreakdownResponse",
    "FailureAnalyticsResponse",
    "ObservabilitySummary",
    "TimeseriesResponse",
]
