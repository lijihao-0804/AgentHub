"""The adapter that supplies thread history to the runtime.

It reads only finished turns, so the same run recomputes the same context after
a durable resume. There is no summarization, no embedding and no memory
extraction here on purpose: history is the earlier turns, verbatim and bounded,
and anything cleverer would be a new abstraction the runtime cannot explain.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.work_layer import ThreadConversation, ThreadTurnContext
from packages.artifacts.models import Artifact
from packages.threads.models import ThreadTurn

_SUCCEEDED = "SUCCEEDED"
MAX_REF_TITLE = 80


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


__all__ = ["SqlAlchemyThreadContextProvider"]
