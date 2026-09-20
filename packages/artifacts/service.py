"""Workspace-scoped management of artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.artifacts.models import Artifact
from packages.artifacts.schemas import validate_artifact_content
from packages.control_plane.rbac import WORKSPACE_READ
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.threads.models import AgentThread

AGENT_RUN = "agent_run"

NOT_FOUND = "ARTIFACT_NOT_FOUND"
NOT_FOUND_MESSAGE = "The artifact was not found."

MAX_TITLE_LENGTH = 300
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
    try:
        return UUID(context.workspace_id)
    except (TypeError, ValueError):
        raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None


def _principal_id(context: WorkspaceExecutionContext) -> UUID:
    try:
        if context.user_id is None:
            raise ValueError
        return UUID(context.user_id)
    except (TypeError, ValueError):
        raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401) from None


def _require(context: WorkspaceExecutionContext, permission: str) -> UUID:
    if permission not in context.permissions:
        raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
    return _workspace_id(context)


def _not_found() -> NoReturn:
    raise AgentHubError(NOT_FOUND, NOT_FOUND_MESSAGE, 404)


def _validated_title(value: str) -> str:
    title = value.strip()
    if not title or len(title) > MAX_TITLE_LENGTH:
        raise AgentHubError("ARTIFACT_INVALID", "The artifact title is invalid.", 422)
    return title


class ArtifactService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def list_for_thread(
        self,
        context: WorkspaceExecutionContext,
        thread_id: UUID,
        *,
        artifact_type: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[Artifact]:
        workspace_id = _require(context, WORKSPACE_READ)
        bounded = max(1, min(limit, MAX_PAGE_SIZE))
        async with self.session_factory() as session:
            await self._require_thread(session, workspace_id, thread_id)
            query = select(Artifact).where(
                Artifact.workspace_id == workspace_id, Artifact.thread_id == thread_id
            )
            if artifact_type is not None:
                query = query.where(Artifact.type == artifact_type)
            query = (
                query.order_by(Artifact.created_at.desc(), Artifact.id.desc())
                .limit(bounded)
                .offset(max(offset, 0))
            )
            return list(await session.scalars(query))

    async def get(self, context: WorkspaceExecutionContext, artifact_id: UUID) -> Artifact:
        workspace_id = _require(context, WORKSPACE_READ)
        async with self.session_factory() as session:
            return await self._load(session, workspace_id, artifact_id)

    async def create(
        self,
        context: WorkspaceExecutionContext,
        thread_id: UUID,
        *,
        artifact_type: str,
        title: str,
        content: Any,
    ) -> Artifact:
        workspace_id = _require(context, AGENT_RUN)
        created_by = _principal_id(context)
        clean_title = _validated_title(title)
        # Validate before touching the thread, so a bad body never costs a
        # lookup, and never lands half-written.
        validated = validate_artifact_content(artifact_type, content)
        async with self.session_factory() as session:
            await self._require_thread(session, workspace_id, thread_id)
            artifact = Artifact(
                workspace_id=workspace_id,
                thread_id=thread_id,
                run_id=None,
                type=artifact_type,
                title=clean_title,
                content=validated,
                created_by=created_by,
            )
            session.add(artifact)
            await session.commit()
            await session.refresh(artifact)
            return artifact

    async def update(
        self,
        context: WorkspaceExecutionContext,
        artifact_id: UUID,
        *,
        title: str | None = None,
        content: Any | None = None,
    ) -> Artifact:
        workspace_id = _require(context, AGENT_RUN)
        async with self.session_factory() as session:
            artifact = await self._load(session, workspace_id, artifact_id)
            if artifact.run_id is not None:
                # An agent-produced artifact is the record of one execution.
                # Editing it would make it no longer a record. The user saves
                # their own copy instead, which has no run.
                raise AgentHubError(
                    "ARTIFACT_NOT_EDITABLE",
                    "An artifact produced by a run cannot be edited; save a copy instead.",
                    409,
                )
            if title is not None:
                artifact.title = _validated_title(title)
            if content is not None:
                artifact.content = validate_artifact_content(artifact.type, content)
            artifact.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(artifact)
            return artifact

    async def delete(self, context: WorkspaceExecutionContext, artifact_id: UUID) -> None:
        workspace_id = _require(context, AGENT_RUN)
        async with self.session_factory() as session:
            artifact = await self._load(session, workspace_id, artifact_id)
            await session.delete(artifact)
            await session.commit()

    async def _load(self, session: AsyncSession, workspace_id: UUID, artifact_id: UUID) -> Artifact:
        artifact = await session.scalar(
            select(Artifact).where(
                Artifact.workspace_id == workspace_id, Artifact.id == artifact_id
            )
        )
        if artifact is None:
            _not_found()
        return artifact

    async def _require_thread(
        self, session: AsyncSession, workspace_id: UUID, thread_id: UUID
    ) -> None:
        exists = await session.scalar(
            select(AgentThread.id).where(
                AgentThread.workspace_id == workspace_id, AgentThread.id == thread_id
            )
        )
        if exists is None:
            raise AgentHubError("THREAD_NOT_FOUND", "The thread was not found.", 404)


__all__ = ["ArtifactService", "NOT_FOUND"]
