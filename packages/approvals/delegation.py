"""Standing instructions for the original run actor's own governed tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.approvals.models import Approval, ApprovalPreference
from packages.control_plane.audit import append_audit
from packages.control_plane.models import (
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.control_plane.rbac import APPROVE_ACTION, resolve_permissions
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.tools.contracts import ToolDefinition, ToolEffect, ToolRisk
from packages.tools.policy import ToolPolicy


class DelegationLevel(StrEnum):
    MANUAL = "MANUAL"
    LOW = "LOW"
    MEDIUM = "MEDIUM"


DEFAULT_DENIAL_LIMIT = 2


@dataclass(frozen=True)
class PreferenceView:
    level: DelegationLevel
    max_auto_denials: int
    identity_ceiling: DelegationLevel
    effective_level: DelegationLevel


async def locked_actor_permissions(
    session: AsyncSession, context: WorkspaceExecutionContext
) -> frozenset[str]:
    """Keep the member and account grants stable until this decision is committed."""
    user_id, workspace_id = UUID(context.user_id), UUID(context.workspace_id)
    org_role = await session.scalar(
        select(OrganizationMembership.role)
        .join(Workspace, Workspace.organization_id == OrganizationMembership.organization_id)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(
            Workspace.id == workspace_id,
            Workspace.organization_id == UUID(context.organization.organization_id),
            OrganizationMembership.user_id == user_id,
            User.is_active.is_(True),
        )
        .with_for_update(read=True, of=(OrganizationMembership, User))
    )
    workspace_role = None
    if org_role == "MEMBER":
        workspace_role = await session.scalar(
            select(WorkspaceMembership.role)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
            .with_for_update(read=True)
        )
    permissions = resolve_permissions(org_role, workspace_role) if org_role else frozenset()
    if not permissions:
        raise AgentHubError("FORBIDDEN", "You do not have access to this workspace.", 403)
    # A restricted invocation can narrow actual grants but can never add to them.
    return permissions & context.permissions


def _view(preference: ApprovalPreference | None, permissions: frozenset[str]) -> PreferenceView:
    level = DelegationLevel(preference.level) if preference else DelegationLevel.MANUAL
    ceiling = DelegationLevel.MEDIUM if APPROVE_ACTION in permissions else DelegationLevel.MANUAL
    return PreferenceView(
        level,
        preference.max_auto_denials if preference else DEFAULT_DENIAL_LIMIT,
        ceiling,
        level if ceiling is DelegationLevel.MEDIUM else ceiling,
    )


async def read_preferences(
    session: AsyncSession, context: WorkspaceExecutionContext
) -> PreferenceView:
    permissions = await locked_actor_permissions(session, context)
    row = await session.get(ApprovalPreference, (UUID(context.workspace_id), UUID(context.user_id)))
    return _view(row, permissions)


async def write_preferences(
    session: AsyncSession,
    context: WorkspaceExecutionContext,
    *,
    level: DelegationLevel,
    max_auto_denials: int,
) -> PreferenceView:
    permissions = await locked_actor_permissions(session, context)
    if level is not DelegationLevel.MANUAL and APPROVE_ACTION not in permissions:
        raise AgentHubError(
            "APPROVAL_DELEGATION_FORBIDDEN", "This level exceeds your approval authority.", 403
        )
    if type(max_auto_denials) is not int or not 0 <= max_auto_denials <= 10:
        raise AgentHubError(
            "INVALID_DENIAL_LIMIT", "The denial limit must be between 0 and 10.", 422
        )
    workspace_id, user_id = UUID(context.workspace_id), UUID(context.user_id)
    values = {"level": level.value, "max_auto_denials": max_auto_denials}
    row = (
        await session.execute(
            insert(ApprovalPreference)
            .values(workspace_id=workspace_id, user_id=user_id, **values)
            .on_conflict_do_update(
                index_elements=[ApprovalPreference.workspace_id, ApprovalPreference.user_id],
                set_=values,
            )
            .returning(ApprovalPreference)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    append_audit(
        session,
        action="approval.preferences.update",
        resource_type="approval_preference",
        resource_id=str(user_id),
        request_id=context.request_id,
        actor_user_id=user_id,
        organization_id=UUID(context.organization.organization_id),
        workspace_id=workspace_id,
        safe_metadata=values,
    )
    response = _view(row, permissions)
    await session.commit()
    return response


async def lock_preferences(
    session: AsyncSession, workspace_id: UUID, user_id: UUID
) -> ApprovalPreference | None:
    return await session.scalar(
        select(ApprovalPreference)
        .where(
            ApprovalPreference.workspace_id == workspace_id, ApprovalPreference.user_id == user_id
        )
        .with_for_update()
    )


async def _preceding_denials(session: AsyncSession, approval: Approval) -> int:
    # A human decision on the same business request ends a denial streak.
    # Separate logical actions still require their own decision and execution claim.
    scope = (
        Approval.workspace_id == approval.workspace_id,
        Approval.requested_by == approval.requested_by,
        Approval.tool_revision_id == approval.tool_revision_id,
        Approval.canonical_args_hash == approval.canonical_args_hash,
    )
    last_human = await session.scalar(
        select(func.max(Approval.decided_at)).where(
            *scope, Approval.decision_source == "HUMAN", Approval.routing_reason.is_not(None)
        )
    )
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Approval)
            .where(
                *scope,
                Approval.decision_source == "DELEGATED",
                Approval.decision_status == "DENIED",
                *([Approval.decided_at > last_human] if last_human else []),
            )
        )
        or 0
    )


async def route_new_approval(
    session: AsyncSession,
    context: WorkspaceExecutionContext,
    approval: Approval,
    definition: ToolDefinition,
) -> None:
    """Route only a new proposal, inside the transaction that inserts it."""
    if ToolPolicy.requires_human(definition):
        approval.routing_reason = "SENSITIVE_OPERATION"
        return
    permissions = await locked_actor_permissions(session, context)
    preference = await lock_preferences(session, approval.workspace_id, approval.requested_by)
    if preference is None or preference.level == DelegationLevel.MANUAL:
        approval.routing_reason = "MANUAL_MODE"
        return
    required = {"workspace_read", "agent_run", "tool_run"}
    if definition.effect is ToolEffect.WRITE:
        required.add("run_action")
    if definition.identity in {"search_knowledge", "query_wiki"}:
        required.add("knowledge_run")
    if not required.issubset(permissions):
        raise AgentHubError(
            "RUN_ACTOR_ACCESS_REVOKED", "The run actor can no longer use this tool.", 403
        )
    if APPROVE_ACTION not in permissions:
        approval.routing_reason = "IDENTITY_CEILING"
        return
    prior = None
    if definition.risk_level is ToolRisk.LOW or preference.level == DelegationLevel.MEDIUM:
        status, reason = "APPROVED", "WITHIN_LEVEL"
    else:
        prior = await _preceding_denials(session, approval)
        if prior >= preference.max_auto_denials:
            approval.routing_reason = "DENIAL_LIMIT_REACHED"
            append_audit(
                session,
                action="approval.escalate",
                resource_type="approval",
                resource_id=str(approval.id),
                request_id=context.request_id,
                actor_user_id=approval.requested_by,
                organization_id=UUID(context.organization.organization_id),
                workspace_id=approval.workspace_id,
                safe_metadata={
                    "logical_action_id": approval.logical_action_id,
                    "tool_revision_id": str(definition.revision_id),
                    "routing_reason": approval.routing_reason,
                    "prior_auto_denials": prior,
                    "max_auto_denials": preference.max_auto_denials,
                    "level": preference.level,
                },
            )
            return
        status, reason = "DENIED", "ABOVE_LEVEL"
    approval.routing_reason = reason
    approval.decision_status, approval.decision_source = status, "DELEGATED"
    # Order decisions using the same database clock after acquiring the lock;
    # workers and human API processes may not share a synchronized machine clock.
    approval.decided_at = await session.scalar(select(func.clock_timestamp()))
    approval.decided_by = approval.requested_by
    append_audit(
        session,
        action="approval.decide",
        resource_type="approval",
        resource_id=str(approval.id),
        request_id=context.request_id,
        actor_user_id=approval.requested_by,
        organization_id=UUID(context.organization.organization_id),
        workspace_id=approval.workspace_id,
        safe_metadata={
            "approval_id": str(approval.id),
            "logical_action_id": approval.logical_action_id,
            "tool_identity": definition.identity,
            "tool_revision_id": str(definition.revision_id),
            "risk_level": definition.risk_level.value,
            "decision": status,
            "decision_source": "DELEGATED",
            "routing_reason": reason,
            "level": preference.level,
            "max_auto_denials": preference.max_auto_denials,
            **({"prior_auto_denials": prior} if prior is not None else {}),
        },
    )
