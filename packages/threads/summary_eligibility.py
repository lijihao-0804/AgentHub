"""Audited, source-bound eligibility decisions for Thread summaries.

An explicit review only establishes a narrow source classification. It does
not itself authorize a model request or prove that all protected facts have
been identified. Missing or stale evidence is never treated as eligible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.summary_source import stable_summary_source_identity
from packages.agent_runtime.work_layer import ThreadHistorySourceManifest
from packages.artifacts.models import Artifact
from packages.control_plane.models import OrganizationMembership, Workspace, WorkspaceMembership
from packages.control_plane.rbac import WORKSPACE_ADMIN, WORKSPACE_READ, resolve_permissions
from packages.core.database import Base
from packages.core.errors.exceptions import AgentHubError
from packages.threads.context import (
    SqlAlchemyThreadContextProvider,
    ThreadHistorySourceIntegrityError,
)
from packages.threads.models import AgentThread, ThreadTurn


class EligibilityDecision(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNKNOWN = "UNKNOWN"


class ThreadSummaryEligibilityRecord(Base):
    """Append-only manual review bound to one stable original source."""

    __tablename__ = "thread_summary_eligibility_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_summary_eligibility_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "turn_id"],
            ["thread_turns.workspace_id", "thread_turns.id"],
            name="fk_summary_eligibility_turn",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_summary_eligibility_source_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "reviewed_from_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_summary_eligibility_review_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="fk_summary_eligibility_reviewer",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "source_identity",
            "revision",
            name="uq_summary_eligibility_revision",
        ),
        CheckConstraint(
            "decision IN ('ELIGIBLE', 'INELIGIBLE')", name="ck_summary_eligibility_decision"
        ),
        CheckConstraint("revision > 0", name="ck_summary_eligibility_revision"),
        CheckConstraint(
            "source_identity ~ '^[0-9a-f]{64}$'", name="ck_summary_eligibility_source_identity"
        ),
        CheckConstraint(
            "(decision = 'ELIGIBLE' AND reason_code = 'ADMIN_REVIEWED') OR "
            "(decision = 'INELIGIBLE' AND reason_code = 'KEEP_EXACT')",
            name="ck_summary_eligibility_reason",
        ),
        Index(
            "ix_summary_eligibility_latest",
            "workspace_id",
            "thread_id",
            "source_identity",
            "revision",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    turn_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    reviewed_from_run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    source_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewed_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    decision: EligibilityDecision
    reason: str
    source_identity: str | None = None
    review_id: UUID | None = None


class SummaryEligibilityService:
    """Read authoritative membership and the Run's own frozen source manifest."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.provider = SqlAlchemyThreadContextProvider(session_factory)

    async def review(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        trigger_run_id: UUID,
        source_id: str,
        actor_id: UUID,
        decision: EligibilityDecision,
    ) -> EligibilityResult:
        if not isinstance(decision, EligibilityDecision) or decision not in {
            EligibilityDecision.ELIGIBLE,
            EligibilityDecision.INELIGIBLE,
        }:
            raise ValueError("a review must be ELIGIBLE or INELIGIBLE")
        async with self.session_factory() as session:
            async with session.begin():
                permissions = await _permissions(session, workspace_id, actor_id)
                if WORKSPACE_ADMIN not in permissions:
                    raise AgentHubError("FORBIDDEN", "Workspace administration is required.", 403)
                source, turn, identity = await self._load_source(
                    session, workspace_id, thread_id, trigger_run_id, source_id
                )
                if decision is EligibilityDecision.ELIGIBLE and (
                    turn.artifact_ids
                    or turn.evidence_registry
                    or turn.source_knowledge_snapshots
                    or turn.source_wiki_publications
                    or any(
                        message.role == "tool" or message.tool_calls for message in turn.messages
                    )
                ):
                    raise AgentHubError(
                        "SUMMARY_SOURCE_REQUIRES_EXACT", "This source must remain exact.", 422
                    )
                revision = await session.scalar(
                    select(func.max(ThreadSummaryEligibilityRecord.revision)).where(
                        ThreadSummaryEligibilityRecord.workspace_id == workspace_id,
                        ThreadSummaryEligibilityRecord.thread_id == thread_id,
                        ThreadSummaryEligibilityRecord.source_identity == identity,
                    )
                )
                row = ThreadSummaryEligibilityRecord(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    turn_id=source.turn_id,
                    run_id=source.run_id,
                    reviewed_from_run_id=trigger_run_id,
                    source_identity=identity,
                    revision=(revision or 0) + 1,
                    decision=decision.value,
                    reason_code="ADMIN_REVIEWED"
                    if decision is EligibilityDecision.ELIGIBLE
                    else "KEEP_EXACT",
                    reviewed_by=actor_id,
                )
                session.add(row)
                await session.flush()
                return EligibilityResult(decision, row.reason_code, identity, row.id)

    async def check(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        trigger_run_id: UUID,
        source_id: str,
        actor_id: UUID,
    ) -> EligibilityResult:
        async with self.session_factory() as session:
            async with session.begin():
                permissions = await _permissions(session, workspace_id, actor_id)
                if WORKSPACE_READ not in permissions:
                    raise AgentHubError("FORBIDDEN", "Workspace access is required.", 403)
                try:
                    source, turn, identity = await self._load_source(
                        session, workspace_id, thread_id, trigger_run_id, source_id
                    )
                except (ThreadHistorySourceIntegrityError, TypeError, ValueError):
                    return EligibilityResult(EligibilityDecision.UNKNOWN, "SOURCE_UNVERIFIED")
                if (
                    turn.artifact_ids
                    and WORKSPACE_READ not in permissions
                    or turn.evidence_registry
                    and "knowledge_run" not in permissions
                    or any(
                        message.role == "tool" or message.tool_calls for message in turn.messages
                    )
                    and "tool_run" not in permissions
                ):
                    return EligibilityResult(EligibilityDecision.UNKNOWN, "SOURCE_UNAUTHORIZED")
                row = await session.scalar(
                    select(ThreadSummaryEligibilityRecord)
                    .where(
                        ThreadSummaryEligibilityRecord.workspace_id == workspace_id,
                        ThreadSummaryEligibilityRecord.thread_id == thread_id,
                        ThreadSummaryEligibilityRecord.source_identity == identity,
                        ThreadSummaryEligibilityRecord.turn_id == source.turn_id,
                        ThreadSummaryEligibilityRecord.run_id == source.run_id,
                    )
                    .order_by(ThreadSummaryEligibilityRecord.revision.desc())
                    .limit(1)
                )
                if row is None:
                    return EligibilityResult(EligibilityDecision.UNKNOWN, "NOT_REVIEWED")
                reviewer_permissions = await _permissions(
                    session, workspace_id, row.reviewed_by, allow_missing=True
                )
                if WORKSPACE_ADMIN not in reviewer_permissions:
                    return EligibilityResult(EligibilityDecision.UNKNOWN, "REVIEWER_REVOKED")
                return EligibilityResult(
                    EligibilityDecision(row.decision), row.reason_code, identity, row.id
                )

    async def _load_source(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        thread_id: UUID,
        trigger_run_id: UUID,
        source_id: str,
    ):
        # The Thread row serializes reviews and protects the initial revision race.
        thread = await session.scalar(
            select(AgentThread)
            .where(AgentThread.workspace_id == workspace_id, AgentThread.id == thread_id)
            .with_for_update()
        )
        trigger = await session.scalar(
            select(AgentRun)
            .where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.thread_id == thread_id,
                AgentRun.id == trigger_run_id,
            )
            .with_for_update(read=True)
        )
        if (
            thread is None
            or trigger is None
            or not isinstance(trigger.effective_thread_history_snapshot, dict)
        ):
            raise ThreadHistorySourceIntegrityError("Frozen Thread source is unavailable.")
        snapshot = trigger.effective_thread_history_snapshot.get("source")
        manifest = ThreadHistorySourceManifest.from_manifest(snapshot)
        scope = manifest.scope
        if (scope.workspace_id, scope.thread_id, scope.before_run_id) != (
            workspace_id,
            thread_id,
            trigger_run_id,
        ):
            raise ThreadHistorySourceIntegrityError("Frozen Thread scope differs.")
        if (
            manifest.status != "COMPLETE"
            or manifest.selected_source_count != manifest.available_eligible_source_count
        ):
            raise ThreadHistorySourceIntegrityError("Frozen source set is incomplete.")
        source = next(
            (item for item in manifest.selected_sources if item.source_id == source_id), None
        )
        if source is None:
            raise ThreadHistorySourceIntegrityError("Source is absent from the frozen manifest.")
        current = await self.provider._m3_current_sequence(
            session, workspace_id=workspace_id, thread_id=thread_id, run_id=trigger_run_id
        )
        if current != scope.before_sequence:
            raise ThreadHistorySourceIntegrityError("Frozen watermark changed.")
        # Lock every mutable persisted component before replaying the manifest.
        await session.scalar(
            select(ThreadTurn.id)
            .where(ThreadTurn.workspace_id == workspace_id, ThreadTurn.id == source.turn_id)
            .with_for_update(read=True)
        )
        await session.scalar(
            select(AgentRun.id)
            .where(AgentRun.workspace_id == workspace_id, AgentRun.id == source.run_id)
            .with_for_update(read=True)
        )
        if source.artifact_ids:
            await session.execute(
                select(Artifact.id)
                .where(Artifact.workspace_id == workspace_id, Artifact.id.in_(source.artifact_ids))
                .order_by(Artifact.id)
                .with_for_update(read=True)
            )
        conversation = await self.provider._materialize_frozen_sources(
            session, scope=scope, manifest=manifest, sources=(source,)
        )
        if len(conversation.turns) != 1:
            raise ThreadHistorySourceIntegrityError("Frozen source could not be replayed.")
        turn = conversation.turns[0]
        return source, turn, stable_summary_source_identity(source, turn)


async def _permissions(
    session: AsyncSession, workspace_id: UUID, actor_id: UUID, *, allow_missing: bool = False
) -> frozenset[str]:
    if not isinstance(workspace_id, UUID) or not isinstance(actor_id, UUID):
        raise TypeError("workspace and actor must be UUIDs")
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        if allow_missing:
            return frozenset()
        raise AgentHubError("FORBIDDEN", "Workspace access is required.", 403)
    org = await session.scalar(
        select(OrganizationMembership)
        .where(
            OrganizationMembership.organization_id == workspace.organization_id,
            OrganizationMembership.user_id == actor_id,
        )
        .with_for_update(read=True)
    )
    if org is None:
        if allow_missing:
            return frozenset()
        raise AgentHubError("FORBIDDEN", "Workspace access is required.", 403)
    member = await session.scalar(
        select(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == actor_id,
        )
        .with_for_update(read=True)
    )
    permissions = resolve_permissions(org.role, member.role if member is not None else None)
    if not permissions and not allow_missing:
        raise AgentHubError("FORBIDDEN", "Workspace access is required.", 403)
    return permissions
