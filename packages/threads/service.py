"""Workspace-scoped management of threads and their turns.

This layer owns authorization, workspace scoping and turn bookkeeping. It never
executes anything: submitting a turn resolves an agent version and hands the
work to the existing :class:`AgentRunService`. Threaded runs share its execution
graph with Playground runs, while thread-specific context and work-layer
integrations add history, history search, artifact recording and memory
extraction where configured.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID, uuid4

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


def _input_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_token(client_token: str | None) -> str:
    """Return the caller's request id, minting one only when it is absent."""

    return (client_token or "").strip() or uuid4().hex


@dataclass(frozen=True, slots=True)
class SubmittedTurn:
    """A turn that has been recorded and the run that is answering it."""

    turn: ThreadTurn
    run_id: UUID
    agent_version_id: UUID
    reused: bool


@dataclass(frozen=True, slots=True)
class PreparedTurnRun:
    """A Turn paired with the Run a retry may dispatch, when one exists."""

    turn: ThreadTurn
    run_id: UUID
    run: AgentRun | None
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
            from packages.agent_runtime.summary_models import ThreadSummaryJob
            from packages.handoffs.models import HandoffCase
            from packages.threads.summary_eligibility import ThreadSummaryEligibilityRecord

            thread = await session.scalar(
                select(AgentThread)
                .where(AgentThread.workspace_id == workspace_id, AgentThread.id == thread_id)
                .with_for_update()
            )
            if thread is None:
                raise AgentHubError("THREAD_NOT_FOUND", "The thread was not found.", 404)
            retained = await session.scalar(
                select(HandoffCase.id).where(
                    HandoffCase.workspace_id == workspace_id, HandoffCase.thread_id == thread_id
                )
            )
            if retained is not None:
                raise AgentHubError(
                    "HANDOFF_EVIDENCE_RETAINED",
                    "The thread contains retained human handoff evidence.",
                    409,
                )
            # Summary review and dispatch records are immutable evidence.  The
            # thread lock also serializes this check with new scoped records.
            summary_job = await session.scalar(
                select(ThreadSummaryJob.id).where(
                    ThreadSummaryJob.workspace_id == workspace_id,
                    ThreadSummaryJob.thread_id == thread_id,
                ).limit(1)
            )
            summary_review = await session.scalar(
                select(ThreadSummaryEligibilityRecord.id).where(
                    ThreadSummaryEligibilityRecord.workspace_id == workspace_id,
                    ThreadSummaryEligibilityRecord.thread_id == thread_id,
                ).limit(1)
            )
            if summary_job is not None or summary_review is not None:
                raise AgentHubError(
                    "SUMMARY_EVIDENCE_RETAINED",
                    "The thread contains retained summary evidence.",
                    409,
                )
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
    ) -> ThreadTurn:
        """Record the question before anything is executed.

        A token identifies one logical request. Reusing it with the same
        normalized body returns the original turn; reusing it for different
        content is a conflict. A new token always creates a new turn, even
        when it deliberately asks the same question again.
        """

        workspace_id = _require(context, AGENT_RUN)
        text = _validated_input(user_input)
        requested_token = client_token.strip() if client_token else None
        if requested_token is not None and (
            not requested_token or len(requested_token) > MAX_CLIENT_TOKEN_LENGTH
        ):
            raise AgentHubError("THREAD_TURN_INVALID", "The client token is invalid.", 422)
        token = _request_token(requested_token)
        digest = _input_hash(text)
        async with self.session_factory() as session:
            # One writer per thread owns "max(sequence) + 1". Without this
            # lock two new tokens can read the same next number; retrying that
            # collision as though it were an idempotency replay would silently
            # discard one request.
            await session.scalar(
                select(AgentThread)
                .where(
                    AgentThread.workspace_id == workspace_id,
                    AgentThread.id == thread_id,
                )
                .with_for_update()
            )
            await self._load(session, workspace_id, thread_id)
            existing = self._require_matching_turn(
                await self._by_token(session, workspace_id, thread_id, token),
                digest=digest,
            )
            if existing is not None:
                session.expunge(existing)
                return existing
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
                input_hash=digest,
            )
            session.add(turn)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = self._require_matching_turn(
                    await self._by_token(session, workspace_id, thread_id, token),
                    digest=digest,
                )
                if existing is not None:
                    session.expunge(existing)
                    return existing
                # A different request raced this one for the same sequence.
                # Retry once now that the winning row is committed.
                next_sequence = await session.scalar(
                    select(func.coalesce(func.max(ThreadTurn.sequence), 0) + 1).where(
                        ThreadTurn.workspace_id == workspace_id,
                        ThreadTurn.thread_id == thread_id,
                    )
                )
                turn.sequence = int(next_sequence or 1)
                session.add(turn)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    existing = self._require_matching_turn(
                        await self._by_token(session, workspace_id, thread_id, token),
                        digest=digest,
                    )
                    if existing is None:
                        raise AgentHubError(
                            "THREAD_TURN_IDEMPOTENCY_CONFLICT",
                            "The turn could not be recorded safely.",
                            409,
                        ) from None
                    session.expunge(existing)
                    return existing
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
        return await self._submit_prepared_turn(
            context,
            thread_id=thread_id,
            agent_version_id=agent_version_id,
            turn=turn,
        )

    async def _submit_prepared_turn(
        self,
        context: WorkspaceExecutionContext,
        *,
        thread_id: UUID,
        agent_version_id: UUID,
        turn: ThreadTurn,
    ) -> SubmittedTurn:
        """Recover or create the one Run bound to a persisted Turn."""

        if self.run_service is None:
            raise AgentHubError("DATABASE_NOT_CONFIGURED", "Runs are not configured.", 503)
        existing_run = await self._turn_run(context, thread_id, turn)
        if existing_run is not None:
            if turn.agent_run_id is None:
                await self.attach_run(context, turn_id=turn.id, run_id=existing_run.id)
                turn.agent_run_id = existing_run.id
            if self._is_unstarted_run(existing_run):
                await self.run_service.execute_prepared_run(
                    context,
                    run_id=existing_run.id,
                )
            return SubmittedTurn(
                turn=turn,
                run_id=existing_run.id,
                agent_version_id=existing_run.agent_version_id,
                reused=True,
            )
        try:
            prepared_run = await self.run_service.prepare_run(
                context,
                agent_version_id=agent_version_id,
                input_text=turn.user_input,
                thread_id=thread_id,
                thread_turn_id=turn.id,
            )
        except IntegrityError as error:
            winner = await self._winner_after_run_conflict(
                context,
                thread_id=thread_id,
                turn=turn,
                error=error,
            )
            await self.attach_run(context, turn_id=turn.id, run_id=winner.id)
            turn.agent_run_id = winner.id
            # The uniqueness race guarantees one Run, not that its owner has
            # started it. Run lease acquisition makes this safe for every
            # contender; a loser immediately observes the winner's result.
            if self._is_unstarted_run(winner):
                await self.run_service.execute_prepared_run(context, run_id=winner.id)
            return SubmittedTurn(
                turn=turn,
                run_id=winner.id,
                agent_version_id=winner.agent_version_id,
                reused=True,
            )
        await self.attach_run(context, turn_id=turn.id, run_id=prepared_run.id)
        turn.agent_run_id = prepared_run.id
        result = await self.run_service.execute_prepared_run(context, run_id=prepared_run.id)
        return SubmittedTurn(
            turn=turn,
            run_id=result.run_id,
            agent_version_id=agent_version_id,
            reused=False,
        )

    async def ensure_stream_run(
        self,
        context: WorkspaceExecutionContext,
        *,
        thread_id: UUID,
        agent_version_id: UUID,
        turn: ThreadTurn,
    ) -> PreparedTurnRun:
        """Claim and prepare the one streaming Run for a persisted Turn.

        The database Turn row is the claim. Every contender looks up the Run by
        ``thread_turn_id``; only the winner that created it streams, and every
        retry replays that Run. There is no grace window or time comparison.
        """

        if self.run_service is None:
            raise AgentHubError("DATABASE_NOT_CONFIGURED", "Runs are not configured.", 503)
        existing = await self._turn_run(context, thread_id, turn)
        if existing is not None:
            if turn.agent_run_id is None:
                await self.attach_run(context, turn_id=turn.id, run_id=existing.id)
                turn.agent_run_id = existing.id
            return PreparedTurnRun(
                turn=turn,
                run_id=existing.id,
                run=existing if self._is_unstarted_run(existing) else None,
                reused=True,
            )
        try:
            prepared = await self.run_service.prepare_stream(
                context,
                agent_version_id=agent_version_id,
                input_text=turn.user_input,
                thread_id=thread_id,
                thread_turn_id=turn.id,
            )
        except IntegrityError as error:
            winner = await self._winner_after_run_conflict(
                context,
                thread_id=thread_id,
                turn=turn,
                error=error,
            )
            await self.attach_run(context, turn_id=turn.id, run_id=winner.id)
            turn.agent_run_id = winner.id
            return PreparedTurnRun(
                turn=turn,
                run_id=winner.id,
                run=winner if self._is_unstarted_run(winner) else None,
                reused=True,
            )
        await self.attach_run(context, turn_id=turn.id, run_id=prepared.id)
        turn.agent_run_id = prepared.id
        return PreparedTurnRun(
            turn=turn,
            run_id=prepared.id,
            run=prepared,
            reused=False,
        )

    async def token_turn(
        self,
        context: WorkspaceExecutionContext,
        thread_id: UUID,
        client_token: str | None,
        *,
        user_input: str,
    ) -> ThreadTurn:
        """Find the turn recorded for a request id, rejecting body changes."""

        return await self._require_token_turn(
            context, thread_id, client_token, user_input=user_input
        )

    # -- internals -------------------------------------------------------

    async def _turn_run(
        self,
        context: WorkspaceExecutionContext,
        thread_id: UUID,
        turn: ThreadTurn,
    ) -> AgentRun | None:
        """Return only a Run that is provably bound to this exact Turn.

        New turns use ``agent_runs.thread_turn_id``. Historical reverse
        pointers are accepted only when 0036 proved them unique and when every
        identity field still agrees. Ambiguous or null historical bindings
        produce an explicit recovery error; this lookup never infers ownership
        from creation time or sequence.
        """

        workspace_id = _workspace_id(context)
        async with self.session_factory() as session:
            stable = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.thread_id == thread_id,
                    AgentRun.thread_turn_id == turn.id,
                )
            )
            if stable is not None:
                if stable.input_text != turn.user_input or (
                    turn.agent_run_id is not None and turn.agent_run_id != stable.id
                ):
                    raise AgentHubError(
                        "THREAD_TURN_RECOVERY_REQUIRED",
                        "The turn's stable and legacy Run bindings disagree.",
                        409,
                    )
                return stable

            if turn.legacy_recovery_required:
                raise AgentHubError(
                    "THREAD_TURN_RECOVERY_REQUIRED",
                    "The historical turn cannot be recovered without operator review.",
                    409,
                )
            if turn.agent_run_id is None:
                return None

            legacy = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.id == turn.agent_run_id,
                )
            )
            if legacy is None:
                raise AgentHubError(
                    "THREAD_TURN_RECOVERY_REQUIRED",
                    "The historical turn references a missing Run.",
                    409,
                )
            if (
                legacy.thread_id != thread_id
                or legacy.input_text != turn.user_input
                or (legacy.thread_turn_id is not None and legacy.thread_turn_id != turn.id)
            ):
                raise AgentHubError(
                    "THREAD_TURN_RECOVERY_REQUIRED",
                    "The historical turn has an inconsistent Run binding.",
                    409,
                )
            owners = await session.scalar(
                select(func.count())
                .select_from(ThreadTurn)
                .where(
                    ThreadTurn.workspace_id == workspace_id,
                    ThreadTurn.id != turn.id,
                    ThreadTurn.agent_run_id == legacy.id,
                )
            )
            if int(owners or 0):
                raise AgentHubError(
                    "THREAD_TURN_RECOVERY_REQUIRED",
                    "The historical Run is shared by more than one Turn.",
                    409,
                )
            return legacy

    async def _winner_after_run_conflict(
        self,
        context: WorkspaceExecutionContext,
        *,
        thread_id: UUID,
        turn: ThreadTurn,
        error: IntegrityError,
    ) -> AgentRun:
        """Resolve only the Run-to-Turn uniqueness race; re-raise anything else."""

        if not self._is_run_turn_conflict(error):
            raise error
        winner = await self._turn_run(context, thread_id, turn)
        if winner is None:
            raise error
        return winner

    @staticmethod
    def _is_run_turn_conflict(error: IntegrityError) -> bool:
        detail = str(getattr(error.orig, "diag", None) or error)
        return "uq_agent_runs_thread_turn" in detail

    @staticmethod
    def _is_unstarted_run(run: AgentRun) -> bool:
        """Only an unclaimed RUNNING row can safely be dispatched again."""

        return (
            getattr(run, "status", None) == "RUNNING"
            and getattr(run, "execution_owner", None) is None
        )

    async def _require_token_turn(
        self,
        context: WorkspaceExecutionContext,
        thread_id: UUID,
        client_token: str | None,
        *,
        user_input: str,
    ) -> ThreadTurn:
        workspace_id = _workspace_id(context)
        text = _validated_input(user_input)
        digest = _input_hash(text)
        token = (client_token or "").strip()
        if not token:
            raise AgentHubError(
                "THREAD_TURN_NOT_FOUND",
                "No submission token was supplied.",
                404,
            )
        async with self.session_factory() as session:
            existing = self._require_matching_turn(
                await self._by_token(session, workspace_id, thread_id, token),
                digest=digest,
            )
            if existing is None:
                raise AgentHubError(
                    "THREAD_TURN_NOT_FOUND",
                    "The matching turn was not found.",
                    404,
                )
            session.expunge(existing)
            return existing

    @staticmethod
    def _require_matching_turn(
        existing: ThreadTurn | None,
        *,
        digest: str,
    ) -> ThreadTurn | None:
        if existing is not None and existing.input_hash != digest:
            raise AgentHubError(
                "THREAD_TURN_IDEMPOTENCY_CONFLICT",
                "The submission token is already bound to different input.",
                409,
            )
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
                    & (AgentRun.thread_turn_id == ThreadTurn.id),
                )
                .where(
                    ThreadTurn.workspace_id == workspace_id,
                    ThreadTurn.thread_id == thread_id,
                )
                .order_by(ThreadTurn.sequence)
            )
            return [(turn, run) for turn, run in rows.all()]


__all__ = ["NOT_FOUND", "PreparedTurnRun", "SubmittedTurn", "ThreadService"]
