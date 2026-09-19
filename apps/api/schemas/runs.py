from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ApprovalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    pending: int
    approved: int
    denied: int
    expired: int
    cancelled: int
    not_started: int
    claimed: int
    succeeded: int
    failed: int
    unknown_outcome: int


class RunListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    trace_id: str
    workspace_id: UUID
    agent_version_id: UUID
    agent_version_number: int
    resolved_spec_hash: str | None
    status: str
    failure_code: str | None
    failure_category: str | None
    created_at: datetime
    started_at: datetime
    completed_at: datetime | None
    duration_ms: float | None
    model_step_count: int
    tool_call_count: int
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_tokens: int | None
    total_cached_tokens: int | None
    total_cost_amount: Decimal | None
    cost_currency: str | None
    cost_is_estimate: bool | None
    approval_summary: ApprovalSummary


class RunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RunListItem]
    next_cursor: str | None


class RunDetail(RunListItem):
    effective_knowledge_snapshots: list[dict[str, Any]]
    trace_url: str | None = None


class TimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    occurred_at: datetime
    kind: str
    status: str
    duration_ms: float | None
    metadata: dict[str, Any]
    failure_code: str | None
    failure_category: str | None


class RunTimelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    workspace_id: UUID
    items: list[TimelineEntry]
