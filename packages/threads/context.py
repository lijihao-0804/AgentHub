"""The adapter that supplies thread history to the runtime.

It reads only finished turns, so the same run recomputes the same context after
a durable resume. There is no summarization, no embedding and no memory
extraction here on purpose: history is the earlier turns, verbatim and bounded,
and anything cleverer would be a new abstraction the runtime cannot explain.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.work_layer import (
    ThreadConversation,
    ThreadHistoryHit,
    ThreadTurnContext,
)
from packages.artifacts.models import Artifact
from packages.core.text import search_terms
from packages.threads.models import ThreadTurn

_SUCCEEDED = "SUCCEEDED"
MAX_REF_TITLE = 80

# How much of a matched turn is quoted back. Long enough to be an answer, short
# enough that five hits cannot swallow the context the search was supposed to
# save.
MAX_HIT_INPUT = 300
MAX_HIT_OUTPUT = 800
# The thread search shares the project-wide tokenizer; it is kept as a module
# level alias because "what counts as a term" is the whole reason this search
# works in Chinese, and a reader looking at the query builder should find it.
_terms = search_terms


def _excerpt(text_value: str, limit: int) -> str:
    text_value = (text_value or "").strip()
    if len(text_value) <= limit:
        return text_value
    return text_value[: limit - 1] + "…"


def _artifact_ref(artifact: Artifact) -> str:
    """One line standing in for a whole artifact.

    Twenty abstracts would eat the budget to tell the model something one line
    already tells it: that a search happened, and roughly what it found. A
    follow-up that really needs the contents searches again.
    """

    title = (artifact.title or "").strip()
    if len(title) > MAX_REF_TITLE:
        title = title[: MAX_REF_TITLE - 1] + "…"
    papers = artifact.content.get("papers") if isinstance(artifact.content, dict) else None
    count = len(papers) if isinstance(papers, list) else 0
    return f'[artifact: {artifact.type} "{title}" ({count} papers)]'


class SqlAlchemyThreadContextProvider:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def conversation(
        self, *, workspace_id: UUID, thread_id: UUID, before_run_id: UUID, max_turns: int
    ) -> ThreadConversation:
        if max_turns <= 0:
            return ThreadConversation()
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(ThreadTurn, AgentRun)
                    .join(
                        AgentRun,
                        (AgentRun.workspace_id == ThreadTurn.workspace_id)
                        & (AgentRun.id == ThreadTurn.agent_run_id),
                    )
                    .where(
                        ThreadTurn.workspace_id == workspace_id,
                        ThreadTurn.thread_id == thread_id,
                        ThreadTurn.agent_run_id.is_not(None),
                        ThreadTurn.agent_run_id != before_run_id,
                        AgentRun.status == _SUCCEEDED,
                        AgentRun.final_output.is_not(None),
                    )
                    .order_by(ThreadTurn.sequence)
                )
            ).all()
            available = len(rows)
            selected = rows[-max_turns:]
            run_ids = [run.id for _, run in selected]
            refs: dict[UUID, list[str]] = {}
            if run_ids:
                artifacts = await session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.workspace_id == workspace_id,
                        Artifact.thread_id == thread_id,
                        Artifact.run_id.in_(run_ids),
                    )
                    .order_by(Artifact.created_at, Artifact.id)
                )
                for artifact in artifacts:
                    if artifact.run_id is not None:
                        refs.setdefault(artifact.run_id, []).append(_artifact_ref(artifact))
            turns = tuple(
                ThreadTurnContext(
                    user_input=turn.user_input,
                    final_output=run.final_output or "",
                    artifact_refs=tuple(refs.get(run.id, ())),
                )
                for turn, run in selected
            )
        return ThreadConversation(turns=turns, turns_available=available)

    async def search(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        before_run_id: UUID,
        query: str,
        limit: int,
    ) -> tuple[ThreadHistoryHit, ...]:
        """Find earlier turns of *this* thread that mention the query terms.

        The scope is deliberately narrow: one thread, one workspace, finished
        turns only, and never the run doing the asking. A tool that could reach
        into a neighbouring thread would be a retrieval feature wearing a
        memory costume, and would need the governance that comes with one.
        """

        terms = _terms(query)
        if not terms or limit <= 0:
            return ()
        conditions = [
            or_(
                ThreadTurn.user_input.ilike(f"%{term}%"),
                AgentRun.final_output.ilike(f"%{term}%"),
            )
            for term in terms
        ]
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(ThreadTurn, AgentRun)
                    .join(
                        AgentRun,
                        (AgentRun.workspace_id == ThreadTurn.workspace_id)
                        & (AgentRun.id == ThreadTurn.agent_run_id),
                    )
                    .where(
                        ThreadTurn.workspace_id == workspace_id,
                        ThreadTurn.thread_id == thread_id,
                        ThreadTurn.agent_run_id.is_not(None),
                        ThreadTurn.agent_run_id != before_run_id,
                        AgentRun.status == _SUCCEEDED,
                        AgentRun.final_output.is_not(None),
                        or_(*conditions),
                    )
                    .order_by(ThreadTurn.sequence)
                )
            ).all()
        scored: list[tuple[int, int, ThreadHistoryHit]] = []
        for turn, run in rows:
            haystack = f"{turn.user_input}\n{run.final_output or ''}".lower()
            matched = tuple(term for term in terms if term in haystack)
            if not matched:
                continue
            scored.append(
                (
                    len(matched),
                    turn.sequence,
                    ThreadHistoryHit(
                        sequence=turn.sequence,
                        user_input=_excerpt(turn.user_input, MAX_HIT_INPUT),
                        final_output=_excerpt(run.final_output or "", MAX_HIT_OUTPUT),
                        matched_terms=matched,
                    ),
                )
            )
        # Most terms matched wins; ties go to the more recent turn, because in a
        # conversation the later statement is the one that survived revision.
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return tuple(hit for _, _, hit in scored[:limit])


__all__ = ["SqlAlchemyThreadContextProvider"]
