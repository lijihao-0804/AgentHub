"""Server-resolved approval actor for evaluation-only approval cases."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.control_plane.models import OrganizationMembership, Workspace
from packages.control_plane.services import TenantService
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import PrincipalContext, WorkspaceExecutionContext
from packages.evaluation.models import EvaluationExperimentRun


class EvaluationApprovalActorProvider:
    """Resolve a real OWNER/ADMIN for ApprovalService; never fabricate permissions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def context_for(
        self, run: EvaluationExperimentRun
    ) -> WorkspaceExecutionContext:
        async with self.session_factory() as session:
            workspace = await session.scalar(
                select(Workspace).where(Workspace.id == run.workspace_id)
            )
            if workspace is None:
                raise AgentHubError(
                    "EVALUATION_APPROVAL_ACTOR_UNAVAILABLE",
                    "The evaluation approval operator is unavailable.",
                    409,
                )
            actor_id = await session.scalar(
                select(OrganizationMembership.user_id)
                .where(
                    OrganizationMembership.organization_id == workspace.organization_id,
                    OrganizationMembership.role.in_({"OWNER", "ADMIN"}),
                    OrganizationMembership.user_id != run.created_by,
                )
                .order_by(OrganizationMembership.role, OrganizationMembership.user_id)
                .limit(1)
            )
            if actor_id is None:
                raise AgentHubError(
                    "EVALUATION_APPROVAL_ACTOR_UNAVAILABLE",
                    "A distinct evaluation approval operator is unavailable.",
                    409,
                )
            principal = PrincipalContext(
                request_id=f"evaluation-approval:{run.id}",
                trace_id=f"evaluation-approval:{run.id}",
                user_id=str(actor_id),
            )
            return (
                await TenantService().get_workspace_access(
                    session,
                    principal=principal,
                    workspace_id=run.workspace_id,
                )
            ).context


__all__ = ["EvaluationApprovalActorProvider"]
