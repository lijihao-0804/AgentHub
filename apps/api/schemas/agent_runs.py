from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class AgentRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_text: str = Field(
        min_length=1,
        max_length=50_000,
        validation_alias=AliasChoices("input", "input_text"),
    )


class AgentRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    agent_version_id: UUID
    status: str
    input_text: str
    final_output: str | None
    failure_code: str | None
    resolved_spec_hash: str | None
    effective_knowledge_snapshots: list[dict[str, Any]]
    model_step_count: int
    tool_call_count: int
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_tokens: int | None
    total_cached_tokens: int | None
    total_cost_amount: Decimal | None
    cost_currency: str | None
    cost_is_estimate: bool | None
    created_by: UUID
    created_at: datetime
    started_at: datetime
    completed_at: datetime | None


class RunStepResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    agent_run_id: UUID
    sequence_number: int
    kind: str
    status: str
    safe_metadata: dict[str, Any]
    created_at: datetime
