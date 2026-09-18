"""Short-transaction Approval persistence and state transitions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.approvals.contracts import (
    ApprovalDecisionStatus,
    ApprovalExecutionStatus,
    canonicalize_arguments,
    compute_logical_action_id,
)
from packages.approvals.models import Approval
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext


class ApprovalService:
    """Owns approval business state; routes and graph nodes call this service."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def create_or_get(
        self,
        context: WorkspaceExecutionContext,
        *,
        run_id: UUID,
        agent_version_id: UUID,
        tool_revision_id: UUID | None,
        tool_identity: str,
        arguments: Mapping[str, Any],
        input_schema: Mapping[str, Any],
        proposal_ordinal: int,
        expires_at: datetime | None = None,
    ) -> Approval:
        workspace_id = _workspace_uuid(context)
        requested_by = _user_uuid(context)
        canonical_arguments, args_hash = canonicalize_arguments(arguments, input_schema)
        logical_action_id = compute_logical_action_id(
            workspace_id=workspace_id,
            run_id=run_id,
            tool_revision_id=tool_revision_id,
            canonical_args_hash=args_hash,
            proposal_ordinal=proposal_ordinal,
        )
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(Approval).where(
                    Approval.workspace_id == workspace_id,
                    Approval.logical_action_id == logical_action_id,
                )
            )
            if existing is not None:
                return existing
            approval = Approval(
                workspace_id=workspace_id,
                run_id=run_id,
                agent_version_id=agent_version_id,
                logical_action_id=logical_action_id,
                tool_revision_id=tool_revision_id,
                tool_identity=tool_identity,
                canonical_arguments=canonical_arguments,
                canonical_args_hash=args_hash,
                requested_by=requested_by,
                expires_at=expires_at,
                idempotency_key=logical_action_id,
            )
            session.add(approval)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(Approval).where(
                        Approval.workspace_id == workspace_id,
                        Approval.logical_action_id == logical_action_id,
                    )
                )
                if existing is None:
                    raise
                return existing
            await session.refresh(approval)
            return approval

    async def get(
        self, context: WorkspaceExecutionContext, approval_id: UUID, *, for_update: bool = False
    ) -> Approval:
        statement = select(Approval).where(
            Approval.workspace_id == _workspace_uuid(context), Approval.id == approval_id
        )
        if for_update:
            statement = statement.with_for_update()
        async with self.session_factory() as session:
            approval = await session.scalar(statement)
            if approval is None:
                raise AgentHubError("APPROVAL_NOT_FOUND", "The approval was not found.", 404)
            return approval

    async def list(self, context: WorkspaceExecutionContext) -> list[Approval]:
        async with self.session_factory() as session:
            result = await session.scalars(
                select(Approval)
                .where(Approval.workspace_id == _workspace_uuid(context))
                .order_by(Approval.created_at, Approval.id)
            )
            return list(result)

    async def decide(
        self,
        context: WorkspaceExecutionContext,
        approval_id: UUID,
        *,
        decision: ApprovalDecisionStatus,
    ) -> Approval:
        if "approve_action" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission to decide approvals.", 403)
        if decision not in {ApprovalDecisionStatus.APPROVED, ApprovalDecisionStatus.DENIED}:
            raise AgentHubError(
                "INVALID_APPROVAL_DECISION", "The approval decision is invalid.", 422
            )
        workspace_id = _workspace_uuid(context)
        user_id = _user_uuid(context)
        async with self.session_factory() as session:
            approval = await session.scalar(
                select(Approval)
                .where(Approval.workspace_id == workspace_id, Approval.id == approval_id)
                .with_for_update()
            )
            if approval is None:
                raise AgentHubError("APPROVAL_NOT_FOUND", "The approval was not found.", 404)
            if approval.decision_status != ApprovalDecisionStatus.PENDING:
                return approval
            if (
                decision is ApprovalDecisionStatus.APPROVED
                and approval.requested_by == user_id
                and context.organization.org_role not in {"OWNER", "ADMIN"}
            ):
                raise AgentHubError(
                    "SELF_APPROVAL_FORBIDDEN", "You cannot approve your own action.", 403
                )
            approval.decision_status = decision.value
            approval.decided_by = user_id
            approval.decided_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(approval)
            return approval

    async def claim_execution(
        self, context: WorkspaceExecutionContext, approval_id: UUID
    ) -> Approval | None:
        """Atomically claim one approved action; loser receives no execution lease."""

        workspace_id = _workspace_uuid(context)
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            result = await session.execute(
                update(Approval)
                .where(
                    Approval.workspace_id == workspace_id,
                    Approval.id == approval_id,
                    Approval.decision_status == ApprovalDecisionStatus.APPROVED,
                    Approval.execution_status == ApprovalExecutionStatus.NOT_STARTED,
                )
                .values(
                    execution_status=ApprovalExecutionStatus.CLAIMED,
                    claimed_at=now,
                    execution_attempt_count=Approval.execution_attempt_count + 1,
                )
                .returning(Approval)
            )
            claimed = result.scalar_one_or_none()
            await session.commit()
            return claimed

    async def complete_execution(
        self,
        context: WorkspaceExecutionContext,
        approval_id: UUID,
        *,
        status: ApprovalExecutionStatus,
        safe_result: dict[str, Any] | None = None,
        failure_code: str | None = None,
        safe_failure_message: str | None = None,
    ) -> Approval:
        if status not in {
            ApprovalExecutionStatus.SUCCEEDED,
            ApprovalExecutionStatus.FAILED,
            ApprovalExecutionStatus.UNKNOWN_OUTCOME,
        }:
            raise AgentHubError("INVALID_EXECUTION_STATUS", "The execution result is invalid.", 422)
        async with self.session_factory() as session:
            approval = await session.scalar(
                select(Approval)
                .where(
                    Approval.workspace_id == _workspace_uuid(context), Approval.id == approval_id
                )
                .with_for_update()
            )
            if approval is None:
                raise AgentHubError("APPROVAL_NOT_FOUND", "The approval was not found.", 404)
            if approval.execution_status in {
                ApprovalExecutionStatus.SUCCEEDED,
                ApprovalExecutionStatus.FAILED,
                ApprovalExecutionStatus.UNKNOWN_OUTCOME,
            }:
                return approval
            approval.execution_status = status.value
            approval.safe_result = safe_result
            approval.failure_code = failure_code
            approval.safe_failure_message = safe_failure_message
            approval.executed_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(approval)
            return approval


def _workspace_uuid(context: WorkspaceExecutionContext) -> UUID:
    try:
        return UUID(context.workspace_id)
    except (TypeError, ValueError) as exc:
        raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401) from exc


def _user_uuid(context: WorkspaceExecutionContext) -> UUID:
    try:
        return UUID(str(context.user_id))
    except (TypeError, ValueError) as exc:
        raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401) from exc


__all__ = ["ApprovalService"]
