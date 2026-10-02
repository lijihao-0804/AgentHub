"""Transactional human handoff lifecycle with fresh server membership checks."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from packages.artifacts.models import Artifact
from packages.artifacts.schemas import SUPPORT_HANDOFF
from packages.control_plane.audit import append_audit
from packages.control_plane.models import (
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.control_plane.rbac import (
    HANDOFF_HANDLE,
    WORKSPACE_ADMIN,
    WORKSPACE_READ,
    resolve_permissions,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.handoffs.models import HandoffCase
from packages.threads.models import AgentThread


def fail(code, message, status=409):
    raise AgentHubError(code, message, status)


async def access(session, context, permission, *, user_id=None):
    """Lock relevant membership rows so revocation cannot race a committed write."""
    workspace_id = UUID(context.workspace_id)
    actor_id = user_id or UUID(context.user_id)
    workspace = await session.get(Workspace, workspace_id)
    user = await session.scalar(select(User).where(User.id == actor_id).with_for_update(read=True))
    if workspace is None or user is None or not user.is_active:
        fail("FORBIDDEN", "Current membership is required.", 403)
    org = await session.scalar(
        select(OrganizationMembership)
        .where(
            OrganizationMembership.organization_id == workspace.organization_id,
            OrganizationMembership.user_id == actor_id,
        )
        .with_for_update(read=True)
    )
    member = await session.scalar(
        select(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == actor_id,
        )
        .with_for_update(read=True)
    )
    if org is None or permission not in resolve_permissions(
        org.role, member.role if member else None
    ):
        fail("FORBIDDEN", "Current membership does not permit this action.", 403)
    return workspace, actor_id


def source_hash(artifact):
    return canonical_json_hash(
        {
            "type": artifact.type,
            "title": artifact.title,
            "content": artifact.content,
            "thread_id": str(artifact.thread_id),
            "run_id": str(artifact.run_id) if artifact.run_id else None,
        }
    )


class HandoffService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def open(self, context, artifact_id):
        async with self.session_factory() as session:
            workspace, actor = await access(session, context, HANDOFF_HANDLE)
            # Thread deletion locks the thread too; then both sides lock the source.
            source = await session.scalar(
                select(Artifact).where(
                    Artifact.workspace_id == workspace.id, Artifact.id == artifact_id
                )
            )
            if source is None:
                fail("ARTIFACT_NOT_FOUND", "The artifact was not found.", 404)
            await session.scalar(
                select(AgentThread)
                .where(AgentThread.workspace_id == workspace.id, AgentThread.id == source.thread_id)
                .with_for_update()
            )
            source = await session.scalar(
                select(Artifact)
                .where(Artifact.workspace_id == workspace.id, Artifact.id == artifact_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if source is None:
                fail("ARTIFACT_NOT_FOUND", "The artifact was not found.", 404)
            if source.type != SUPPORT_HANDOFF:
                fail("HANDOFF_SOURCE_INVALID", "A support handoff artifact is required.", 422)
            existing = await session.scalar(
                select(HandoffCase).where(
                    HandoffCase.source_artifact_id == source.id,
                    HandoffCase.workspace_id == workspace.id,
                )
            )
            if existing is not None:
                return existing
            row = HandoffCase(
                workspace_id=workspace.id,
                thread_id=source.thread_id,
                source_artifact_id=source.id,
                source_hash=source_hash(source),
                created_by=actor,
            )
            session.add(row)
            await session.flush()
            self.audit(session, context, workspace, actor, row, "opened")
            await session.commit()
            await session.refresh(row)
            return row

    async def list_for_thread(self, context, thread_id, *, limit=50, offset=0):
        async with self.session_factory() as session:
            workspace, _ = await access(session, context, WORKSPACE_READ)
            thread = await session.scalar(
                select(AgentThread.id).where(
                    AgentThread.workspace_id == workspace.id, AgentThread.id == thread_id
                )
            )
            if thread is None:
                fail("THREAD_NOT_FOUND", "The thread was not found.", 404)
            return list(
                await session.scalars(
                    select(HandoffCase)
                    .where(
                        HandoffCase.workspace_id == workspace.id, HandoffCase.thread_id == thread_id
                    )
                    .order_by(HandoffCase.created_at.desc(), HandoffCase.id.desc())
                    .limit(max(1, min(limit, 200)))
                    .offset(max(0, offset))
                )
            )

    async def get_for_artifact(self, context, artifact_id):
        async with self.session_factory() as session:
            workspace, _ = await access(session, context, WORKSPACE_READ)
            exists = await session.scalar(
                select(Artifact.id).where(
                    Artifact.workspace_id == workspace.id, Artifact.id == artifact_id
                )
            )
            if exists is None:
                fail("ARTIFACT_NOT_FOUND", "The artifact was not found.", 404)
            return await session.scalar(
                select(HandoffCase).where(
                    HandoffCase.workspace_id == workspace.id,
                    HandoffCase.source_artifact_id == artifact_id,
                )
            )

    async def assignees(self, context):
        async with self.session_factory() as session:
            workspace, _ = await access(session, context, WORKSPACE_ADMIN)
            rows = await session.execute(
                select(User, OrganizationMembership.role, WorkspaceMembership.role)
                .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
                .outerjoin(
                    WorkspaceMembership,
                    (WorkspaceMembership.user_id == User.id)
                    & (WorkspaceMembership.workspace_id == workspace.id),
                )
                .where(
                    OrganizationMembership.organization_id == workspace.organization_id,
                    User.is_active.is_(True),
                )
                .order_by(User.email, User.id)
            )
            return [
                {"user_id": user.id, "email": user.email}
                for user, org, member in rows
                if HANDOFF_HANDLE in resolve_permissions(org, member)
            ]

    async def assign(self, context, case_id, *, expected_version, assignee_id):
        return await self.change(
            context, case_id, expected_version, "assigned", assignee_id=assignee_id
        )

    async def claim(self, context, case_id, *, expected_version):
        return await self.change(context, case_id, expected_version, "claimed")

    async def close(self, context, case_id, *, expected_version, reason, unresolved_items):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            fail("HANDOFF_INVALID", "A bounded closure reason is required.", 422)
        if (
            not isinstance(unresolved_items, list)
            or len(unresolved_items) > 50
            or any(
                not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in unresolved_items
            )
        ):
            fail("HANDOFF_INVALID", "Unresolved items are invalid.", 422)
        return await self.change(
            context,
            case_id,
            expected_version,
            "closed",
            reason=reason.strip(),
            unresolved_items=[v.strip() for v in unresolved_items],
        )

    async def change(self, context, case_id, expected_version, action, **payload):
        if type(expected_version) is not int or expected_version < 1:
            fail("HANDOFF_INVALID", "A positive integer version is required.", 422)
        async with self.session_factory() as session:
            workspace, actor = await access(
                session, context, WORKSPACE_ADMIN if action == "assigned" else HANDOFF_HANDLE
            )
            row = await session.scalar(
                select(HandoffCase)
                .where(HandoffCase.workspace_id == workspace.id, HandoffCase.id == case_id)
                .with_for_update()
            )
            if row is None:
                fail("HANDOFF_NOT_FOUND", "The handoff was not found.", 404)
            request_hash = (
                canonical_json_hash(
                    {"actor": str(actor), "expected_version": expected_version, **payload}
                )
                if action == "closed"
                else None
            )
            if (
                action == "closed"
                and row.status == "CLOSED"
                and row.close_request_hash == request_hash
            ):
                return row
            if row.version != expected_version:
                fail("HANDOFF_VERSION_CONFLICT", "The handoff changed; refresh before retrying.")
            if row.status == "CLOSED":
                fail("HANDOFF_STATE_CONFLICT", "The handoff is already closed.")
            now = datetime.now(UTC)
            if action == "assigned":
                await access(session, context, HANDOFF_HANDLE, user_id=payload["assignee_id"])
                row.assignee_id = payload["assignee_id"]
                row.claimed_by = None
                row.claimed_at = None
                row.status = "ASSIGNED"
            elif action == "claimed":
                if row.status not in {"OPEN", "ASSIGNED"}:
                    fail("HANDOFF_STATE_CONFLICT", "The handoff is already being handled.")
                if row.status == "ASSIGNED" and row.assignee_id != actor:
                    fail("FORBIDDEN", "Only the assigned member can claim this handoff.", 403)
                row.assignee_id = row.claimed_by = actor
                row.claimed_at = now
                row.status = "IN_PROGRESS"
            else:
                if row.status != "IN_PROGRESS" or row.claimed_by != actor:
                    fail("FORBIDDEN", "Only the current handler can close this handoff.", 403)
                row.status = "CLOSED"
                row.closed_by = actor
                row.closed_at = now
                row.closure_reason = payload["reason"]
                row.unresolved_items = payload["unresolved_items"]
                row.close_request_hash = request_hash
            row.version += 1
            row.updated_at = now
            self.audit(session, context, workspace, actor, row, action)
            await session.commit()
            await session.refresh(row)
            return row

    @staticmethod
    def audit(session, context, workspace, actor, row, action):
        append_audit(
            session,
            action="handoff." + action,
            resource_type="handoff",
            resource_id=str(row.id),
            workspace_id=workspace.id,
            organization_id=workspace.organization_id,
            actor_user_id=actor,
            request_id=context.request_id,
            safe_metadata={
                "source_artifact_id": str(row.source_artifact_id),
                "source_hash": row.source_hash,
                "version": row.version,
                "status": row.status,
                "assignee_id": str(row.assignee_id) if row.assignee_id else None,
                "closure_hash": row.close_request_hash,
            },
        )
