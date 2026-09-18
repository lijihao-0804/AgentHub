"""Small, idempotent reconciliation operations for durable approval state."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.approvals.contracts import ApprovalDecisionStatus
from packages.approvals.models import Approval
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext


class ApprovalReconciliationService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def reconcile_run(
        self,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        *,
        checkpoint_exists: bool | None = None,
    ) -> AgentRun:
        if "agent_run" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
        async with self.session_factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(AgentRun.workspace_id == UUID(context.workspace_id), AgentRun.id == run_id)
                .with_for_update()
            )
            if run is None:
                raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
            approvals = list(
                await session.scalars(
                    select(Approval).where(
                        Approval.workspace_id == UUID(context.workspace_id),
                        Approval.run_id == run_id,
                    )
                )
            )
            pending = [
                approval
                for approval in approvals
                if approval.decision_status == ApprovalDecisionStatus.PENDING
            ]
            if run.status == "RUNNING" and pending:
                if checkpoint_exists is False:
                    run.status = "NEEDS_ATTENTION"
                    run.failure_code = "APPROVAL_CHECKPOINT_MISSING"
                else:
                    run.status = "WAITING_APPROVAL"
            if run.status == "WAITING_APPROVAL" and checkpoint_exists is False:
                run.status = "NEEDS_ATTENTION"
                run.failure_code = "APPROVAL_CHECKPOINT_MISSING"
            if run.status in {"NEEDS_ATTENTION", "CANCELLED"} and run.completed_at is None:
                run.completed_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(run)
            return run


__all__ = ["ApprovalReconciliationService"]
