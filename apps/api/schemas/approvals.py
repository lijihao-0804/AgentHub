from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.control_plane.rbac import APPROVE_ACTION
from packages.core.execution_context.models import WorkspaceExecutionContext


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

    @classmethod
    def for_reader(cls, approval, context: WorkspaceExecutionContext) -> ApprovalResponse:
        response = cls.model_validate(approval)
        if "agent_run" in context.permissions or APPROVE_ACTION in context.permissions:
            return response
        return response.model_copy(
            update={"canonical_arguments": {}, "safe_result": None, "safe_failure_message": None}
        )


class ApprovalDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval: ApprovalResponse
    run_id: UUID
    run_status: str


class ApprovalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ApprovalResponse]
    total: int


class ApprovalDenyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)
