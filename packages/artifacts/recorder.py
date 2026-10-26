"""The one recorder every application shares.

There is no research recorder and no incident recorder. There is a loop over
projections, and a projection is the only thing an application adds when it
wants its tool results kept. That is the same claim the templates package
makes about prompts, tested here against a different part of the stack: if a
fifth application needs its own recorder class, the abstraction was wrong.

A projection that does not recognise a call returns ``None`` and the call is
simply not kept. That silence is deliberate -- an unrecognised tool result must
never fail a run, because the run's job was to answer the user, not to file.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.work_layer import RecordedToolCall
from packages.artifacts.models import Artifact
from packages.artifacts.projections import PROJECTIONS, BuiltArtifact
from packages.artifacts.schemas import validate_artifact_content
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError

logger = logging.getLogger(__name__)

Projection = Callable[[RecordedToolCall, UUID], BuiltArtifact | None]


class ToolResultArtifactRecorder:
    """The work-layer side of :class:`RunArtifactRecorder`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        projections: tuple[Projection, ...] = PROJECTIONS,
    ) -> None:
        self.session_factory = session_factory
        self.projections = projections

    def _build(self, call: RecordedToolCall, run_id: UUID) -> BuiltArtifact | None:
        for projection in self.projections:
            built = projection(call, run_id)
            if built is not None:
                # First match wins, and the order in PROJECTIONS is therefore
                # part of the contract. In practice they match on disjoint tool
                # names, so the ordering only decides what happens if two
                # applications ever claim the same one -- which they should not.
                return built
        return None

    @staticmethod
    def logical_call_id(call: RecordedToolCall, artifact_type: str) -> str:
        """Stable key for one projection, independent of provider call ids.

        Runtime calls carry checkpoint-derived identities that separate
        repeated same-tool calls in one model round. External callers without
        that identity use a best-effort fallback. The fallback includes the
        canonical arguments, tool identity, audit step and projection type; it
        cannot distinguish two identical calls made at the same audit step, so
        callers needing that guarantee must supply ``logical_call_id``.
        """

        if call.logical_call_id is not None:
            return call.logical_call_id
        return canonical_json_hash(
            {
                "schema": 2,
                "run_step_sequence": call.step_sequence,
                "tool_identity": call.tool_identity,
                "arguments": call.arguments,
                "artifact_type": artifact_type,
            }
        )

    async def record(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        run_id: UUID,
        created_by: UUID,
        calls: tuple[RecordedToolCall, ...],
    ) -> None:
        pending: list[Artifact] = []
        for call in calls:
            built = self._build(call, run_id)
            if built is None:
                continue
            try:
                validated = validate_artifact_content(built.type, built.content)
            except AgentHubError:
                # A result that cannot be represented is dropped, with a log
                # line. Refusing it here is better than storing a malformed
                # record the UI would later have to defend against.
                logger.warning(
                    "dropping unrepresentable %s result from %s in run %s",
                    built.type,
                    call.tool_identity,
                    run_id,
                )
                continue
            pending.append(
                Artifact(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    logical_call_id=self.logical_call_id(call, built.type),
                    type=built.type,
                    title=built.title,
                    content=validated,
                    created_by=created_by,
                )
            )
        if not pending:
            return
        async with self.session_factory() as session:
            for artifact in pending:
                existing = await session.scalar(
                    select(Artifact.id).where(
                        Artifact.workspace_id == workspace_id,
                        Artifact.run_id == run_id,
                        Artifact.logical_call_id == artifact.logical_call_id,
                    )
                )
                if existing is not None:
                    continue
                try:
                    async with session.begin_nested():
                        session.add(artifact)
                        await session.flush()
                except IntegrityError:
                    # A concurrent replay won. The unique key is the arbiter,
                    # so losing this savepoint must not discard other
                    # projections in the same batch.
                    continue
            await session.commit()


__all__ = ["ToolResultArtifactRecorder"]
