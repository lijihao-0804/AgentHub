from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    run_id: UUID
    agent_version_id: UUID
    logical_action_id: str
    tool_revision_id: UUID | None
    tool_identity: str
    canonical_arguments: dict[str, Any]
    canonical_args_hash: str
    decision_status: str
    execution_status: str
    requested_by: UUID
    decided_by: UUID | None
    decided_at: datetime | None
    claimed_at: datetime | None
    executed_at: datetime | None
    expires_at: datetime | None
    failure_code: str | None
    safe_failure_message: str | None
    safe_result: dict[str, Any] | None
    execution_attempt_count: int
    created_at: datetime
    updated_at: datetime


class ApprovalDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval: ApprovalResponse
    run_id: UUID
    run_status: str
