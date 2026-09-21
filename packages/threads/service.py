"""Workspace-scoped management of threads and their turns.

This layer owns authorization, workspace scoping and turn bookkeeping. It never
executes anything: submitting a turn resolves an agent version and hands the
work to the existing :class:`AgentRunService`, so a threaded run takes exactly
the same path a Playground run takes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import Agent, AgentRun, AgentVersion
from packages.agent_runtime.runtime import AgentRunService
from packages.control_plane.rbac import WORKSPACE_READ
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.threads import kinds
from packages.threads.models import AgentThread, ThreadTurn

AGENT_RUN = "agent_run"

NOT_FOUND = "THREAD_NOT_FOUND"
NOT_FOUND_MESSAGE = "The thread was not found."

MAX_TITLE_LENGTH = 200
MAX_INPUT_LENGTH = 32_000
MAX_CLIENT_TOKEN_LENGTH = 64
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
    # The same answer whether the thread belongs to another workspace or to
    # nobody at all: a 404 that varied would be an existence oracle.
    raise AgentHubError(NOT_FOUND, NOT_FOUND_MESSAGE, 404)


def _validated_title(value: str) -> str:
    title = value.strip()
    if not title or len(title) > MAX_TITLE_LENGTH:
        raise AgentHubError("THREAD_INVALID", "The thread title is invalid.", 422)
    return title


def _validated_input(value: str) -> str:
    text = value.strip()
    if not text or len(text) > MAX_INPUT_LENGTH:
        raise AgentHubError("THREAD_TURN_INVALID", "The turn input is invalid.", 422)
    return text


@dataclass(frozen=True, slots=True)
class SubmittedTurn:
    """A turn that has been recorded and the run that is answering it."""

    turn: ThreadTurn
    run_id: UUID
    agent_version_id: UUID
    reused: bool


class ThreadService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        run_service: AgentRunService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.run_service = run_service

    # -- threads ---------------------------------------------------------

    async def create_thread(
        self,
        context: WorkspaceExecutionContext,
        *,
        agent_id: UUID,
        title: str,
        kind: str = kinds.GENERAL,
    ) -> AgentThread:
        workspace_id = _require(context, AGENT_RUN)
        created_by = _principal_id(context)
        clean_title = _validated_title(title)
        clean_kind = kinds.validate_kind(kind)
        async with self.session_factory() as session:
            agent = await session.scalar(
                select(Agent).where(Agent.workspace_id == workspace_id, Agent.id == agent_id)
            )
            if agent is None:
                # Reported as a missing thread target rather than a missing
                # agent, for the same non-disclosure reason.
                raise AgentHubError("AGENT_NOT_FOUND", "The agent was not found.", 404)
            thread = AgentThread(
                workspace_id=workspace_id,
                agent_id=agent_id,
                title=clean_title,
                kind=clean_kind,
                created_by=created_by,
            )
            session.add(thread)
            await session.commit()
            await session.refresh(thread)
            return thread

    async def list_threads(
        self,
        context: WorkspaceExecutionContext,
        *,
        agent_id: UUID | None = None,
        kind: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[AgentThread]:
        workspace_id = _require(context, WORKSPACE_READ)
        bounded = max(1, min(limit, MAX_PAGE_SIZE))
        async with self.session_factory() as session:
            query = select(AgentThread).where(AgentThread.workspace_id == workspace_id)
            if agent_id is not None:
                query = query.where(AgentThread.agent_id == agent_id)
            if kind is not None:
                query = query.where(AgentThread.kind == kinds.validate_kind(kind))
            query = (
                query.order_by(AgentThread.updated_at.desc(), AgentThread.id.desc())
                .limit(bounded)
                .offset(max(offset, 0))
            )
            return list(await session.scalars(query))

    async def get_thread(self, context: WorkspaceExecutionContext, thread_id: UUID) -> AgentThread:
        workspace_id = _require(context, WORKSPACE_READ)
        async with self.session_factory() as session:
            thread = await self._load(session, workspace_id, thread_id)
            return thread

    async def update_thread(
        self, context: WorkspaceExecutionContext, thread_id: UUID, *, title: str
    ) -> AgentThread:
        workspace_id = _require(context, AGENT_RUN)
        clean_title = _validated_title(title)
        async with self.session_factory() as session:
            thread = await self._load(session, workspace_id, thread_id)
            thread.title = clean_title
            thread.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(thread)
            return thread

    async def delete_thread(self, context: WorkspaceExecutionContext, thread_id: UUID) -> None:
        workspace_id = _require(context, AGENT_RUN)
        async with self.session_factory() as session:
            thread = await self._load(session, workspace_id, thread_id)
            await session.delete(thread)
            await session.commit()

    # -- turns -----------------------------------------------------------

    async def list_turns(
        self,
        context: WorkspaceExecutionContext,
        thread_id: UUID,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[ThreadTurn]:
        workspace_id = _require(context, WORKSPACE_READ)
        bounded = max(1, min(limit, MAX_PAGE_SIZE))
        async with self.session_factory() as session:
            await self._load(session, workspace_id, thread_id)
            return list(
                await session.scalars(
                    select(ThreadTurn)
                    .where(
                        ThreadTurn.workspace_id == workspace_id,
                        ThreadTurn.thread_id == thread_id,
                    )
                    .order_by(ThreadTurn.sequence)
                    .limit(bounded)
                    .offset(max(offset, 0))
                )
            )

    async def resolve_agent_version(
        self, context: WorkspaceExecutionContext, thread_id: UUID
    ) -> tuple[AgentThread, UUID]:
        """Find the version this thread's next turn should run against.

        The thread stores an agent, not a version. Resolving at submission time
        is what lets a long thread pick up a newer published version, and what
        keeps the run — not the thread — the reproducible unit.
        """

        workspace_id = _require(context, AGENT_RUN)
        async with self.session_factory() as session:
            thread = await self._load(session, workspace_id, thread_id)
            version_id = await session.scalar(
                select(AgentVersion.id)
                .where(
                    AgentVersion.workspace_id == workspace_id,
                    AgentVersion.agent_id == thread.agent_id,
                )
                .order_by(AgentVersion.version_number.desc())
                .limit(1)
            )
            if version_id is None:
                raise AgentHubError(
                    "AGENT_VERSION_NOT_FOUND",
                    "The agent has no published version to run.",
                    409,
                )
            session.expunge(thread)
            return thread, version_id

    async def open_turn(
        self,
        context: WorkspaceExecutionContext,
        thread_id: UUID,
        *,
        user_input: str,
        client_token: str | None = None,
    ) -> ThreadTurn | None:
        """Record the question before anything is executed.

        Returns ``None`` when ``client_token`` has already been used in this
        thread: a retried submission must find the turn it already created
        rather than turn one follow-up question into two runs.
        """

        workspace_id = _require(context, AGENT_RUN)
        text = _validated_input(user_input)
        token = client_token.strip() if client_token else None
        if token is not None and (not token or len(token) > MAX_CLIENT_TOKEN_LENGTH):
            raise AgentHubError("THREAD_TURN_INVALID", "The client token is invalid.", 422)
        async with self.session_factory() as session:
            await self._load(session, workspace_id, thread_id)
            if token is not None:
                existing = await self._by_token(session, workspace_id, thread_id, token)
                if existing is not None:
                    return None
            next_sequence = await session.scalar(
                select(func.coalesce(func.max(ThreadTurn.sequence), 0) + 1).where(
                    ThreadTurn.workspace_id == workspace_id,
                    ThreadTurn.thread_id == thread_id,
                )
            )
            turn = ThreadTurn(
                workspace_id=workspace_id,
                thread_id=thread_id,
                sequence=int(next_sequence or 1),
                user_input=text,
                client_token=token,
            )
            session.add(turn)
            try:
                await session.commit()
            except IntegrityError:
                # Either two retries raced on the token, or two tabs raced on
                # the sequence. Both are the caller asking again, not an error.
                await session.rollback()
                return None
            await session.refresh(turn)
            session.expunge(turn)
            return turn

    async def attach_run(
        self, context: WorkspaceExecutionContext, *, turn_id: UUID, run_id: UUID
    ) -> None:
        workspace_id = _require(context, AGENT_RUN)
        async with self.session_factory() as session:
            turn = await session.scalar(
                select(ThreadTurn).where(
                    ThreadTurn.workspace_id == workspace_id, ThreadTurn.id == turn_id
                )
            )
            if turn is None:
                _not_found()
            turn.agent_run_id = run_id
            thread = await session.scalar(
                select(AgentThread).where(
                    AgentThread.workspace_id == workspace_id,
                    AgentThread.id == turn.thread_id,
                )
            )
            if thread is not None:
                thread.updated_at = datetime.now(UTC)
            await session.commit()

    async def submit_turn(
        self,
        context: WorkspaceExecutionContext,
        thread_id: UUID,
        *,
        user_input: str,
        client_token: str | None = None,
    ) -> SubmittedTurn:
        """Record a turn and run it to completion."""

        if self.run_service is None:
            raise AgentHubError("DATABASE_NOT_CONFIGURED", "Runs are not configured.", 503)
        thread, agent_version_id = await self.resolve_agent_version(context, thread_id)
        turn = await self.open_turn(
            context, thread_id, user_input=user_input, client_token=client_token
        )
        if turn is None:
            reused = await self._require_token_turn(context, thread_id, client_token)
            return SubmittedTurn(
                turn=reused,
                run_id=reused.agent_run_id or UUID(int=0),
                agent_version_id=agent_version_id,
                reused=True,
            )
        result = await self.run_service.run(
            context,
            agent_version_id=agent_version_id,
            input_text=turn.user_input,
            thread_id=thread.id,
        )
        await self.attach_run(context, turn_id=turn.id, run_id=result.run_id)
        turn.agent_run_id = result.run_id
        return SubmittedTurn(
            turn=turn,
            run_id=result.run_id,
            agent_version_id=agent_version_id,
            reused=False,
        )

    # -- internals -------------------------------------------------------

    async def _require_token_turn(
        self, context: WorkspaceExecutionContext, thread_id: UUID, client_token: str | None
    ) -> ThreadTurn:
        workspace_id = _workspace_id(context)
        token = (client_token or "").strip()
        async with self.session_factory() as session:
            existing = (
                await self._by_token(session, workspace_id, thread_id, token) if token else None
            )
            if existing is None:
                raise AgentHubError(
                    "THREAD_TURN_CONFLICT",
                    "The turn could not be recorded; retry the submission.",
                    409,
                )
            session.expunge(existing)
            return existing

    async def _by_token(
        self, session: AsyncSession, workspace_id: UUID, thread_id: UUID, token: str
    ) -> ThreadTurn | None:
        return await session.scalar(
            select(ThreadTurn).where(
                ThreadTurn.workspace_id == workspace_id,
                ThreadTurn.thread_id == thread_id,
                ThreadTurn.client_token == token,
            )
        )

    async def _load(
        self, session: AsyncSession, workspace_id: UUID, thread_id: UUID
    ) -> AgentThread:
        thread = await session.scalar(
            select(AgentThread).where(
                AgentThread.workspace_id == workspace_id, AgentThread.id == thread_id
            )
        )
        if thread is None:
            _not_found()
        return thread

    async def turn_summaries(
        self, context: WorkspaceExecutionContext, thread_id: UUID
    ) -> list[tuple[ThreadTurn, AgentRun | None]]:
        """Turns with the run that answered each, for the conversation view."""

        workspace_id = _require(context, WORKSPACE_READ)
        async with self.session_factory() as session:
            await self._load(session, workspace_id, thread_id)
            rows = await session.execute(
                select(ThreadTurn, AgentRun)
                .outerjoin(
                    AgentRun,
                    (AgentRun.workspace_id == ThreadTurn.workspace_id)
                    & (AgentRun.id == ThreadTurn.agent_run_id),
                )
                .where(
                    ThreadTurn.workspace_id == workspace_id,
                    ThreadTurn.thread_id == thread_id,
                )
                .order_by(ThreadTurn.sequence)
            )
            return [(turn, run) for turn, run in rows.all()]


__all__ = ["NOT_FOUND", "SubmittedTurn", "ThreadService"]
