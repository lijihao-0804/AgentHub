"""Reading and overriding what an agent remembers, from the outside.

The store in ``packages.memory.store`` owns its own sessions because the
runtime calls it from places where no request session exists. This service is
the request-shaped half: one open session, permission checks, and the workspace
isolation rule that a memory belonging to another workspace is reported as
absent rather than as forbidden.

Two permissions, both of which already exist: ``workspace_read`` to look and
``agent_edit`` to override. Being allowed to change what an agent believes is
the same authority as being allowed to change its prompt, so it is the same
permission -- a separate one would be a new thing to grant, forget to grant,
and grant too widely.

Nothing here deletes. ``invalidate`` is how a human says "stop using this", and
the row stays so that a run which already used it still explains itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.models import Agent
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.memory.models import MEMORY_KINDS, MEMORY_STATUSES, WorkspaceMemory
from packages.memory.store import ACTIVE, INVALIDATED

MAX_PAGE_SIZE = 100
# Long enough for a phrase, short enough that nobody pushes a novel through
# LIKE. Memory content itself is capped at 400 characters.
MAX_SEARCH_LENGTH = 200


def _like_pattern(search: str | None) -> str | None:
    """Turn a user's phrase into a LIKE pattern that means what they typed.

    ``%`` and ``_`` are ordinary characters in a memory, so they are escaped
    rather than honoured: a search for "50%" must not match everything. The
    backslash used to escape them has to be escaped first, or the escape
    character itself becomes unsearchable.
    """

    if search is None:
        return None
    text = search.strip()[:MAX_SEARCH_LENGTH]
    if not text:
        return None
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass(frozen=True, slots=True)
class MemoryPage:
    items: tuple[WorkspaceMemory, ...]
    total: int


class MemoryAdminService:
    async def list_for_agent(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        *,
        agent_id: UUID,
        status: str | None = None,
        kind: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MemoryPage:
        self._require_permission(context, "workspace_read")
        workspace_id = UUID(context.workspace_id)
        await self._require_agent(session, workspace_id=workspace_id, agent_id=agent_id)
        if status is not None and status not in MEMORY_STATUSES:
            raise AgentHubError("VALIDATION_ERROR", "The memory status is invalid.", 422)
        if kind is not None and kind not in MEMORY_KINDS:
            raise AgentHubError("VALIDATION_ERROR", "The memory kind is invalid.", 422)
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        offset = max(0, offset)

        conditions = [
            WorkspaceMemory.workspace_id == workspace_id,
            WorkspaceMemory.agent_id == agent_id,
        ]
        if status is not None:
            conditions.append(WorkspaceMemory.status == status)
        if kind is not None:
            conditions.append(WorkspaceMemory.kind == kind)
        pattern = _like_pattern(search)
        if pattern is not None:
            # Substring, not the term overlap the runtime selects with. Someone
            # hunting for the memory that made an answer wrong types a fragment
            # of the sentence they saw, and expects that fragment to match.
            conditions.append(WorkspaceMemory.content.ilike(pattern, escape="\\"))
        total = int(
            await session.scalar(
                select(func.count()).select_from(WorkspaceMemory).where(*conditions)
            )
            or 0
        )
        rows = (
            await session.scalars(
                select(WorkspaceMemory)
                .where(*conditions)
                # ACTIVE sorts before INVALIDATED before SUPERSEDED
                # alphabetically, which happens to be the order a human wants:
                # what it believes now, then what was overridden, then history.
                .order_by(
                    WorkspaceMemory.status,
                    WorkspaceMemory.salience.desc(),
                    WorkspaceMemory.created_at.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return MemoryPage(items=tuple(rows), total=total)

    async def invalidate(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        *,
        agent_id: UUID,
        memory_id: UUID,
    ) -> WorkspaceMemory:
        self._require_permission(context, "agent_edit")
        memory = await self._require_memory(
            session, context, agent_id=agent_id, memory_id=memory_id
        )
        if memory.status == INVALIDATED:
            return memory
        await session.execute(
            update(WorkspaceMemory)
            .where(
                WorkspaceMemory.workspace_id == memory.workspace_id,
                WorkspaceMemory.id == memory.id,
            )
            .values(status=INVALIDATED)
        )
        await session.commit()
        await session.refresh(memory)
        return memory

    async def reactivate(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        *,
        agent_id: UUID,
        memory_id: UUID,
    ) -> WorkspaceMemory:
        self._require_permission(context, "agent_edit")
        memory = await self._require_memory(
            session, context, agent_id=agent_id, memory_id=memory_id
        )
        if memory.status != INVALIDATED:
            raise AgentHubError(
                "MEMORY_NOT_INVALIDATED",
                "Only an invalidated memory can be reactivated.",
                409,
            )
        try:
            await session.execute(
                update(WorkspaceMemory)
                .where(
                    WorkspaceMemory.workspace_id == memory.workspace_id,
                    WorkspaceMemory.id == memory.id,
                )
                .values(status=ACTIVE, superseded_by_id=None)
            )
            await session.commit()
        except IntegrityError:
            # The agent learned the same thing again while this one was off.
            # Reactivating would put two identical ACTIVE rows in front of the
            # model, which is exactly what the partial unique index prevents.
            await session.rollback()
            raise AgentHubError(
                "MEMORY_ALREADY_ACTIVE",
                "An identical memory is already active for this agent.",
                409,
            ) from None
        await session.refresh(memory)
        return memory

    # -- internals ---------------------------------------------------------

    async def _require_agent(
        self, session: AsyncSession, *, workspace_id: UUID, agent_id: UUID
    ) -> None:
        exists = await session.scalar(
            select(Agent.id).where(Agent.workspace_id == workspace_id, Agent.id == agent_id)
        )
        if exists is None:
            raise AgentHubError("AGENT_NOT_FOUND", "The agent was not found.", 404)

    async def _require_memory(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        *,
        agent_id: UUID,
        memory_id: UUID,
    ) -> WorkspaceMemory:
        workspace_id = UUID(context.workspace_id)
        await self._require_agent(session, workspace_id=workspace_id, agent_id=agent_id)
        memory = await session.scalar(
            select(WorkspaceMemory).where(
                WorkspaceMemory.workspace_id == workspace_id,
                WorkspaceMemory.agent_id == agent_id,
                WorkspaceMemory.id == memory_id,
            )
        )
        if memory is None:
            # 404 rather than 403 even when the row exists in another
            # workspace: a distinguishable error is an existence oracle.
            raise AgentHubError("MEMORY_NOT_FOUND", "The memory was not found.", 404)
        return memory

    @staticmethod
    def _require_permission(context: WorkspaceExecutionContext, permission: str) -> None:
        if permission not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)


__all__ = ["MAX_PAGE_SIZE", "MAX_SEARCH_LENGTH", "MemoryAdminService", "MemoryPage"]
