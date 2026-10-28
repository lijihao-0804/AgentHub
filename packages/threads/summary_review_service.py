"""Trusted persistence and revalidation for protected Thread-source reviews.

Application API:

* ``record_review`` accepts an authenticated actor, source IDs, and exact
  character ranges. It obtains real workspace-admin eligibility through
  ``SummaryEligibilityService.review`` and persists the rebuilt manifest.
* ``restore_review`` reloads one exact review revision, rechecks current
  eligibility and membership, and reconstructs excerpt text from Thread rows.
* ``attach_review_to_job`` fixes that revision to one summary job.
* ``SummaryReviewBindingVerifier`` implements the existing summary verifier
  protocol and revalidates at dispatch, response validation, and publication.

No API accepts an ``is_admin`` flag, reviewer identity, eligibility revision, or
review revision authority from the request. Database rows contain coordinates,
hashes, and audit metadata rather than duplicate source text. This service does
not log request or source content.

Remaining semantic limit: an administrator attests that the excerpt request is
complete. Exact offsets and source reconstruction do not prove that every
naturally occurring protected fact was noticed. The stored completeness status
therefore remains ``ATTESTED_NOT_PROVEN`` and this module does not enable the
runtime default verifier or relaxation of summary quality gates.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.summary_models import ThreadSummaryJob, ThreadSummaryJobSource
from packages.agent_runtime.summary_prompt import (
    SummaryCompletedAction,
    SummaryInputContext,
    SummaryPromptSource,
    SummaryRequiredReference,
)
from packages.agent_runtime.summary_service import (
    NewSummarySource,
    SummaryJobBinding,
    SummaryTrustDecision,
    SummaryVerifierPhase,
)
from packages.agent_runtime.summary_source import stable_summary_source_identity
from packages.agent_runtime.work_layer import (
    ThreadHistorySourceManifest,
    ThreadTurnContext,
)
from packages.control_plane.models import OrganizationMembership, Workspace, WorkspaceMembership
from packages.control_plane.rbac import WORKSPACE_ADMIN, WORKSPACE_READ, resolve_permissions
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.threads.context import (
    SqlAlchemyThreadContextProvider,
    ThreadHistorySourceIntegrityError,
)
from packages.threads.models import AgentThread, ThreadTurn
from packages.threads.summary_eligibility import (
    EligibilityDecision,
    EligibilityResult,
    SummaryEligibilityService,
    ThreadSummaryEligibilityRecord,
)
from packages.threads.summary_review_manifest import (
    SummaryProtectedExcerptRequest,
    SummaryProtectedReviewManifest,
    SummaryReviewDecision,
    SummaryReviewManifestError,
    SummarySourceReviewAssertion,
    build_summary_review_manifest,
)
from packages.threads.summary_review_models import (
    SUMMARY_REVIEW_BASIS,
    SUMMARY_REVIEW_COMPLETENESS,
    SUMMARY_REVIEW_STORAGE_VERSION,
    SummaryReviewExcerpt,
    SummaryReviewJobBinding,
    SummaryReviewRecord,
    SummaryReviewSource,
)

SUMMARY_REVIEW_ID_SCOPE_KEY: Final = "summary_review_id"
SUMMARY_REVIEW_REVISION_SCOPE_KEY: Final = "summary_review_revision"
SUMMARY_REVIEW_HASH_SCOPE_KEY: Final = "summary_review_hash"


class SummaryReviewPermissionPolicy(StrEnum):
    """Current-state permission policy for the first durable review contract."""

    CURRENT_MEMBERSHIP = "CURRENT_MEMBERSHIP"


@dataclass(frozen=True, slots=True)
class CreateSummaryReview:
    """A server-authenticated request to review every selected frozen source."""

    workspace_id: UUID
    thread_id: UUID
    trigger_run_id: UUID
    actor_id: UUID
    source_ids: tuple[str, ...]
    excerpts: tuple[SummaryProtectedExcerptRequest, ...]
    protected_manifest_complete: bool

    def __post_init__(self) -> None:
        for name in ("workspace_id", "thread_id", "trigger_run_id", "actor_id"):
            if not isinstance(getattr(self, name), UUID):
                raise TypeError(f"{name} must be a UUID")
        source_ids = tuple(self.source_ids)
        excerpts = tuple(self.excerpts)
        if not source_ids or any(
            not isinstance(source_id, str) or not source_id for source_id in source_ids
        ):
            raise ValueError("source_ids must contain at least one non-empty source ID")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_ids must be unique")
        if any(
            not isinstance(excerpt, SummaryProtectedExcerptRequest) for excerpt in excerpts
        ):
            raise TypeError("excerpts must contain SummaryProtectedExcerptRequest values")
        if self.protected_manifest_complete is not True:
            raise ValueError("protected_manifest_complete must be explicitly true")
        object.__setattr__(self, "source_ids", source_ids)
        object.__setattr__(self, "excerpts", excerpts)


@dataclass(frozen=True, slots=True, repr=False)
class SummaryReviewSnapshot:
    """A rebuilt review and the exact text needed by trusted application code."""

    record: SummaryReviewRecord
    manifest: SummaryProtectedReviewManifest
    summary_sources: tuple[NewSummarySource, ...]
    materialized_turns: tuple[ThreadTurnContext, ...] = field(repr=False)

    @property
    def review_id(self) -> UUID:
        return self.record.id

    @property
    def revision(self) -> int:
        return self.record.revision

    @property
    def review_hash(self) -> str:
        return self.record.review_hash

    @property
    def protected_facts(self):
        return self.manifest.for_prompt()


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedJobReview:
    """Internal verifier result containing the job and rebuilt review."""

    job: ThreadSummaryJob
    job_sources: tuple[ThreadSummaryJobSource, ...]
    snapshot: SummaryReviewSnapshot


SummaryContextBuilder = Callable[
    [SummaryReviewSnapshot, ThreadSummaryJob, SummaryJobBinding],
    SummaryInputContext,
]


def summary_review_prompt_source_content(turn: ThreadTurnContext) -> str:
    """Return the exact user/assistant exchange used by the trusted verifier."""

    if not isinstance(turn, ThreadTurnContext):
        raise TypeError("turn must be ThreadTurnContext")
    return f"USER:\n{turn.user_input}\n\nASSISTANT:\n{turn.final_output}"


def summary_review_context_builder(
    *,
    permission_snapshot: str,
    task_goal: str,
    completed_actions: Sequence[SummaryCompletedAction],
    required_references: Sequence[SummaryRequiredReference],
    source_budget_tokens: int,
    final_view_budget_tokens: int,
) -> SummaryContextBuilder:
    """Create a trusted context hook for ``SummaryReviewBindingVerifier``.

    The permission snapshot and non-protected manifests must already come from
    authoritative server state. The verifier binds their permission snapshot to
    the job and rebuilds every source and protected excerpt before dispatch.
    """

    actions = tuple(completed_actions)
    references = tuple(required_references)

    def build(
        snapshot: SummaryReviewSnapshot,
        job: ThreadSummaryJob,
        binding: SummaryJobBinding,
    ) -> SummaryInputContext:
        del binding
        sources = tuple(
            SummaryPromptSource(
                source_id=stored.source_id,
                workspace_id=str(job.workspace_id),
                thread_id=str(job.thread_id),
                identity=stored.source_identity_hash,
                revision=stored.revision_sha256,
                sequence=stored.sequence,
                watermark=job.source_watermark,
                role="exchange",
                speaker="user_and_assistant",
                occurred_at="UNKNOWN",
                content=summary_review_prompt_source_content(turn),
                original_ref=stored.original_ref,
                summary_eligible=True,
                exchange_complete=True,
            )
            for stored, turn in zip(
                snapshot.summary_sources,
                snapshot.materialized_turns,
                strict=True,
            )
        )
        return SummaryInputContext(
            workspace_id=str(job.workspace_id),
            thread_id=str(job.thread_id),
            permission_snapshot=permission_snapshot,
            task_goal=task_goal,
            prefix_first_sequence=sources[0].sequence,
            prefix_last_sequence=sources[-1].sequence,
            watermark=job.source_watermark,
            sources=sources,
            protected_facts=snapshot.protected_facts,
            completed_actions=actions,
            required_references=references,
            source_budget_tokens=source_budget_tokens,
            target_budget_tokens=job.target_budget_tokens,
            final_view_budget_tokens=final_view_budget_tokens,
            prefix_complete=True,
            protected_manifest_complete=True,
            action_manifest_complete=True,
            reference_manifest_complete=True,
        )

    return build


class SummaryReviewService:
    """Persist and reload protected reviews from authoritative PostgreSQL state."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.eligibility = SummaryEligibilityService(session_factory)
        self.provider = SqlAlchemyThreadContextProvider(session_factory)

    async def record_review(self, command: CreateSummaryReview) -> SummaryReviewSnapshot:
        """Create the next immutable review revision for one complete source set."""

        if not isinstance(command, CreateSummaryReview):
            raise TypeError("command must be CreateSummaryReview")
        requested_sources = {
            request.source_id for request in command.excerpts
        }.difference(command.source_ids)
        if requested_sources:
            raise AgentHubError(
                "SUMMARY_REVIEW_SOURCE_INVALID",
                "A protected excerpt cites a source outside the reviewed source set.",
                422,
            )

        # All eligibility revisions and the immutable review commit together.
        lock_key = f"{SUMMARY_REVIEW_STORAGE_VERSION}:{command.workspace_id}:{command.thread_id}"
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": lock_key},
                )
                return await self._record_review_serialized(session, command)

    async def _record_review_serialized(
        self, session: AsyncSession, command: CreateSummaryReview,
    ) -> SummaryReviewSnapshot:
        """Create one review atomically while holding its Thread review lock."""

        eligibility_ids: list[UUID] = []
        for source_id in command.source_ids:
            reviewed = await self.eligibility.review_in_session(
                session,
                workspace_id=command.workspace_id,
                thread_id=command.thread_id,
                trigger_run_id=command.trigger_run_id,
                source_id=source_id,
                actor_id=command.actor_id,
                decision=EligibilityDecision.ELIGIBLE,
            )
            if reviewed.review_id is None or reviewed.decision is not EligibilityDecision.ELIGIBLE:
                raise AgentHubError(
                    "SUMMARY_REVIEW_ELIGIBILITY_FAILED",
                    "A selected source did not receive an eligible admin review.",
                    409,
                )
            eligibility_ids.append(reviewed.review_id)

        await self._lock_permissions(
            session,
            workspace_id=command.workspace_id,
            actor_id=command.actor_id,
            reviewer_ids=(command.actor_id,),
            require_actor_admin=True,
        )
        manifest, turns = await self._lock_frozen_scope(
            session,
            workspace_id=command.workspace_id,
            thread_id=command.thread_id,
            trigger_run_id=command.trigger_run_id,
        )
        manifest_ids = tuple(source.source_id for source in manifest.selected_sources)
        if command.source_ids != manifest_ids:
            raise AgentHubError(
                "SUMMARY_REVIEW_SOURCE_SET_INVALID",
                "The reviewed sources must exactly match the complete frozen manifest.",
                409,
            )
        fixed_rows = await self._load_fixed_eligibility(
            session,
            workspace_id=command.workspace_id,
            thread_id=command.thread_id,
            trigger_run_id=command.trigger_run_id,
            eligibility_ids=tuple(eligibility_ids),
        )
        await self._assert_latest_eligibility(
            session,
            workspace_id=command.workspace_id,
            thread_id=command.thread_id,
            fixed_rows=fixed_rows,
        )
        assertions = self._assertions_from_rows(fixed_rows)
        rebuilt = self._build_manifest(
            manifest=manifest,
            turns=turns,
            assertions=assertions,
            excerpts=command.excerpts,
            workspace_id=command.workspace_id,
            thread_id=command.thread_id,
            trigger_run_id=command.trigger_run_id,
        )
        source_payloads, excerpt_payloads = self._storage_payloads(
            manifest=manifest,
            turns=turns,
            fixed_rows=fixed_rows,
            rebuilt=rebuilt,
        )
        revision = await session.scalar(
            select(func.max(SummaryReviewRecord.revision)).where(
                SummaryReviewRecord.workspace_id == command.workspace_id,
                SummaryReviewRecord.thread_id == command.thread_id,
            )
        )
        record_id = uuid4()
        manifest_json = manifest.to_manifest()
        audit_json = rebuilt.audit.to_record()
        review_hash = self._review_hash(
            workspace_id=command.workspace_id,
            thread_id=command.thread_id,
            trigger_run_id=command.trigger_run_id,
            revision=(revision or 0) + 1,
            review_basis=SUMMARY_REVIEW_BASIS,
            manifest_json=manifest_json,
            audit_json=audit_json,
            sources=source_payloads,
            excerpts=excerpt_payloads,
        )
        record = SummaryReviewRecord(
            id=record_id,
            workspace_id=command.workspace_id,
            thread_id=command.thread_id,
            trigger_run_id=command.trigger_run_id,
            revision=(revision or 0) + 1,
            before_sequence=manifest.scope.before_sequence,
            source_count=len(source_payloads),
            excerpt_count=len(excerpt_payloads),
            source_manifest_sha256=manifest.manifest_sha256,
            review_hash=review_hash,
            review_basis=SUMMARY_REVIEW_BASIS,
            completeness_status=SUMMARY_REVIEW_COMPLETENESS,
            contract_version=SUMMARY_REVIEW_STORAGE_VERSION,
            protected_manifest_attested=True,
            reviewer_admin_verified=True,
            manifest_json=manifest_json,
            audit_json=audit_json,
            reviewed_by=command.actor_id,
        )
        session.add(record)
        await session.flush()
        stored_sources = self._stored_sources(
            record=record,
            fixed_rows=fixed_rows,
            payloads=source_payloads,
        )
        stored_excerpts = self._stored_excerpts(
            record=record,
            payloads=excerpt_payloads,
        )
        # The database verifies every excerpt against its persisted
        # source row, so make that parent write visible first.
        session.add_all(stored_sources)
        await session.flush()
        session.add_all(stored_excerpts)
        await session.flush()
        return self._snapshot(
            record=record,
            stored_sources=tuple(stored_sources),
            manifest=manifest,
            rebuilt=rebuilt,
            turns=turns,
        )


    async def restore_review(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        review_id: UUID,
        revision: int,
        actor_id: UUID,
    ) -> SummaryReviewSnapshot:
        """Reload one exact revision after checking current authorization."""

        for name, value, expected in (
            ("workspace_id", workspace_id, UUID),
            ("thread_id", thread_id, UUID),
            ("review_id", review_id, UUID),
            ("actor_id", actor_id, UUID),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{name} must be a UUID")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ValueError("revision must be a positive integer")
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                record, stored_sources, stored_excerpts = await self._load_exact_review(
                    session, workspace_id=workspace_id, thread_id=thread_id,
                    review_id=review_id, revision=revision,
                )
                await self._check_current_eligibility(
                    session, workspace_id=workspace_id, thread_id=thread_id,
                    trigger_run_id=record.trigger_run_id, actor_id=actor_id,
                    sources=stored_sources,
                )
                await self._lock_permissions(
                    session,
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    reviewer_ids=tuple(source.reviewed_by for source in stored_sources),
                )
                manifest, turns = await self._lock_frozen_scope(
                    session,
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    trigger_run_id=record.trigger_run_id,
                )
                return await self._rebuild_and_assert(
                    session,
                    record=record,
                    stored_sources=stored_sources,
                    stored_excerpts=stored_excerpts,
                    manifest=manifest,
                    turns=turns,
                )

    async def attach_review_to_job(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        job_id: UUID,
        review_id: UUID,
        revision: int,
    ) -> SummaryReviewJobBinding:
        """Persist the sole fixed review binding for one summary job."""

        async with self.session_factory() as session:
            job = await session.scalar(
                select(ThreadSummaryJob).where(
                    ThreadSummaryJob.workspace_id == workspace_id,
                    ThreadSummaryJob.thread_id == thread_id,
                    ThreadSummaryJob.id == job_id,
                )
            )
            if job is None:
                raise AgentHubError(
                    "SUMMARY_JOB_NOT_FOUND", "The summary job was not found.", 404
                )
            actor_id = job.created_by
        snapshot = await self.restore_review(
            workspace_id=workspace_id,
            thread_id=thread_id,
            review_id=review_id,
            revision=revision,
            actor_id=actor_id,
        )
        async with self.session_factory() as session:
            async with session.begin():
                await self._lock_permissions(
                    session,
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    reviewer_ids=tuple(
                        source.reviewed_by
                        for source in await self._load_source_rows(
                            session,
                            workspace_id=workspace_id,
                            thread_id=thread_id,
                            review_id=review_id,
                        )
                    ),
                )
                job = await session.scalar(
                    select(ThreadSummaryJob)
                    .where(
                        ThreadSummaryJob.workspace_id == workspace_id,
                        ThreadSummaryJob.thread_id == thread_id,
                        ThreadSummaryJob.id == job_id,
                    )
                    .with_for_update(read=True)
                )
                if job is None:
                    raise AgentHubError(
                        "SUMMARY_JOB_NOT_FOUND", "The summary job was not found.", 404
                    )
                existing = await session.scalar(
                    select(SummaryReviewJobBinding).where(
                        SummaryReviewJobBinding.workspace_id == workspace_id,
                        SummaryReviewJobBinding.thread_id == thread_id,
                        SummaryReviewJobBinding.job_id == job_id,
                    )
                )
                if existing is not None:
                    if (
                        existing.review_id != review_id
                        or existing.review_revision != revision
                    ):
                        raise AgentHubError(
                            "SUMMARY_REVIEW_JOB_ALREADY_BOUND",
                            "The summary job is already bound to another review revision.",
                            409,
                        )
                    return existing
                job_sources = await self._load_job_sources(session, job)
                self._assert_job_matches_snapshot(job, job_sources, snapshot)
                self._assert_scope_binding(job, snapshot)
                binding = SummaryReviewJobBinding(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    job_id=job_id,
                    review_id=review_id,
                    review_revision=revision,
                )
                session.add(binding)
                await session.flush()
                return binding

    async def verify_job_binding(
        self,
        session: AsyncSession,
        binding: SummaryJobBinding,
    ) -> VerifiedJobReview:
        """Revalidate the job's fixed review without replacing it with latest."""

        stored_binding = await session.scalar(
            select(SummaryReviewJobBinding).where(
                SummaryReviewJobBinding.workspace_id == binding.workspace_id,
                SummaryReviewJobBinding.thread_id == binding.thread_id,
                SummaryReviewJobBinding.job_id == binding.job_id,
            )
        )
        if stored_binding is None:
            raise AgentHubError(
                "SUMMARY_REVIEW_BINDING_MISSING",
                "The summary job has no protected review binding.",
                409,
            )
        job = await session.scalar(
            select(ThreadSummaryJob).where(
                ThreadSummaryJob.workspace_id == binding.workspace_id,
                ThreadSummaryJob.thread_id == binding.thread_id,
                ThreadSummaryJob.id == binding.job_id,
            )
        )
        if job is None:
            raise AgentHubError(
                "SUMMARY_JOB_NOT_FOUND", "The summary job was not found.", 404
            )
        self._assert_binding_matches_job(binding, job)
        record, stored_sources, stored_excerpts = await self._load_exact_review(
            session,
            workspace_id=binding.workspace_id,
            thread_id=binding.thread_id,
            review_id=stored_binding.review_id,
            revision=stored_binding.review_revision,
        )
        await self._check_current_eligibility(
            session,
            workspace_id=binding.workspace_id,
            thread_id=binding.thread_id,
            trigger_run_id=record.trigger_run_id,
            actor_id=job.created_by,
            sources=stored_sources,
        )
        await self._lock_permissions(
            session,
            workspace_id=binding.workspace_id,
            actor_id=job.created_by,
            reviewer_ids=tuple(source.reviewed_by for source in stored_sources),
        )
        manifest, turns = await self._lock_frozen_scope(
            session,
            workspace_id=binding.workspace_id,
            thread_id=binding.thread_id,
            trigger_run_id=record.trigger_run_id,
        )
        snapshot = await self._rebuild_and_assert(
            session,
            record=record,
            stored_sources=stored_sources,
            stored_excerpts=stored_excerpts,
            manifest=manifest,
            turns=turns,
        )
        job_sources = await self._load_job_sources(session, job)
        self._assert_job_matches_snapshot(job, job_sources, snapshot)
        self._assert_scope_binding(job, snapshot)
        trigger = await session.get(AgentRun, job.trigger_run_id)
        frozen = trigger.effective_thread_history_snapshot if trigger is not None else None
        plan = frozen.get("plan") if isinstance(frozen, dict) else None
        decisions = plan.get("decisions") if isinstance(plan, dict) else None
        if not isinstance(decisions, list):
            raise self._stale_error("The frozen history plan is unavailable.")
        for source in job_sources:
            matches = [
                item for item in decisions
                if isinstance(item, dict) and item.get("group_id") == source.source_id
            ]
            if (
                len(matches) != 1
                or matches[0].get("category") != "OPTIONAL"
                or matches[0].get("eligibility") != "ELIGIBLE"
            ):
                raise self._stale_error(
                    "The frozen plan does not authorize summarizing this source."
                )
        return VerifiedJobReview(job=job, job_sources=job_sources, snapshot=snapshot)

    def verifier(
        self,
        context_builder: SummaryContextBuilder | None = None,
    ) -> SummaryReviewBindingVerifier:
        """Return an adapter matching ``SummaryBindingVerifier``."""

        return SummaryReviewBindingVerifier(self, context_builder=context_builder)

    async def _check_current_eligibility(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        trigger_run_id: UUID,
        actor_id: UUID,
        sources: Sequence[SummaryReviewSource],
    ) -> None:
        for source in sources:
            try:
                current: EligibilityResult = await self.eligibility.check_in_session(
                    session,
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    trigger_run_id=trigger_run_id,
                    source_id=source.source_id,
                    actor_id=actor_id,
                )
            except ThreadHistorySourceIntegrityError:
                raise self._stale_error(
                    "The reviewed source is no longer reconstructible."
                ) from None
            except AgentHubError as error:
                if error.status_code == 403:
                    raise AgentHubError(
                        "SUMMARY_REVIEW_PERMISSION_REVOKED",
                        "Current workspace permission does not authorize this review.",
                        403,
                    ) from None
                raise
            if current.reason == "REVIEWER_REVOKED":
                raise AgentHubError(
                    "SUMMARY_REVIEW_PERMISSION_REVOKED",
                    "A review author no longer has workspace-admin permission.",
                    403,
                )
            if (
                current.decision is not EligibilityDecision.ELIGIBLE
                or current.review_id != source.eligibility_review_id
                or current.source_identity != source.source_identity
            ):
                raise self._stale_error(
                    "The fixed source eligibility revision is no longer authoritative."
                )

    async def _lock_frozen_scope(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        trigger_run_id: UUID,
    ) -> tuple[ThreadHistorySourceManifest, tuple[ThreadTurnContext, ...]]:
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
            raise self._stale_error("The frozen Thread scope is unavailable.")
        try:
            manifest = ThreadHistorySourceManifest.from_manifest(
                trigger.effective_thread_history_snapshot.get("source")
            )
        except (TypeError, ValueError):
            raise self._stale_error("The frozen Thread scope is invalid.") from None
        scope = manifest.scope
        if (scope.workspace_id, scope.thread_id, scope.before_run_id) != (
            workspace_id,
            thread_id,
            trigger_run_id,
        ):
            raise self._stale_error("The frozen Thread scope differs.")
        if not manifest.selected_sources:
            raise AgentHubError(
                "SUMMARY_REVIEW_SOURCE_COUNT_INVALID",
                "A protected review requires at least one frozen source.",
                422,
            )
        await self._lock_selected_source_rows(
            session,
            manifest=manifest,
        )
        current_sequence = await self.provider._m3_current_sequence(
            session,
            workspace_id=workspace_id,
            thread_id=thread_id,
            run_id=trigger_run_id,
        )
        if current_sequence != scope.before_sequence:
            raise self._stale_error("The frozen Thread watermark changed.")
        try:
            conversation = await self.provider._materialize_frozen_sources(
                session,
                scope=scope,
                manifest=manifest,
                sources=manifest.selected_sources,
            )
        except (ThreadHistorySourceIntegrityError, TypeError, ValueError):
            raise self._stale_error("The frozen Thread sources changed.") from None
        return manifest, conversation.turns

    async def _lock_selected_source_rows(
        self,
        session: AsyncSession,
        *,
        manifest: ThreadHistorySourceManifest,
    ) -> None:
        turn_ids = tuple(source.turn_id for source in manifest.selected_sources)
        run_ids = tuple(source.run_id for source in manifest.selected_sources)
        locked_turns = set(
            await session.scalars(
                select(ThreadTurn.id)
                .where(
                    ThreadTurn.workspace_id == manifest.scope.workspace_id,
                    ThreadTurn.thread_id == manifest.scope.thread_id,
                    ThreadTurn.id.in_(turn_ids),
                )
                .order_by(ThreadTurn.id)
                .with_for_update(read=True)
            )
        )
        locked_runs = set(
            await session.scalars(
                select(AgentRun.id)
                .where(
                    AgentRun.workspace_id == manifest.scope.workspace_id,
                    AgentRun.id.in_(run_ids),
                )
                .order_by(AgentRun.id)
                .with_for_update(read=True)
            )
        )
        if locked_turns != set(turn_ids) or locked_runs != set(run_ids):
            raise self._stale_error("A frozen source row is unavailable.")

    async def _lock_permissions(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        reviewer_ids: Sequence[UUID],
        require_actor_admin: bool = False,
    ) -> None:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            raise self._permission_error()
        actors = tuple(sorted(set((actor_id, *reviewer_ids)), key=str))
        permissions_by_actor: dict[UUID, frozenset[str]] = {}
        for user_id in actors:
            organization_membership = await session.scalar(
                select(OrganizationMembership)
                .where(
                    OrganizationMembership.organization_id == workspace.organization_id,
                    OrganizationMembership.user_id == user_id,
                )
                .with_for_update(read=True)
            )
            workspace_membership = await session.scalar(
                select(WorkspaceMembership)
                .where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == user_id,
                )
                .with_for_update(read=True)
            )
            permissions_by_actor[user_id] = resolve_permissions(
                organization_membership.role if organization_membership is not None else "",
                workspace_membership.role if workspace_membership is not None else None,
            )
        actor_permissions = permissions_by_actor.get(actor_id, frozenset())
        if WORKSPACE_READ not in actor_permissions:
            raise self._permission_error()
        if require_actor_admin and WORKSPACE_ADMIN not in actor_permissions:
            raise AgentHubError(
                "FORBIDDEN", "Workspace administration is required.", 403
            )
        if any(
            WORKSPACE_ADMIN not in permissions_by_actor.get(reviewer_id, frozenset())
            for reviewer_id in reviewer_ids
        ):
            raise AgentHubError(
                "SUMMARY_REVIEW_PERMISSION_REVOKED",
                "A review author no longer has workspace-admin permission.",
                403,
            )

    async def _load_fixed_eligibility(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        trigger_run_id: UUID,
        eligibility_ids: tuple[UUID, ...],
    ) -> tuple[ThreadSummaryEligibilityRecord, ...]:
        rows = tuple(
            await session.scalars(
                select(ThreadSummaryEligibilityRecord)
                .where(
                    ThreadSummaryEligibilityRecord.workspace_id == workspace_id,
                    ThreadSummaryEligibilityRecord.thread_id == thread_id,
                    ThreadSummaryEligibilityRecord.id.in_(eligibility_ids),
                )
                .order_by(ThreadSummaryEligibilityRecord.id)
            )
        )
        if len(rows) != len(eligibility_ids):
            raise self._stale_error("An eligibility review row disappeared.")
        if any(
            row.decision != EligibilityDecision.ELIGIBLE.value
            or row.reason_code != "ADMIN_REVIEWED"
            or row.reviewed_from_run_id != trigger_run_id
            for row in rows
        ):
            raise self._stale_error("An eligibility review is not valid for this scope.")
        return rows

    async def _assert_latest_eligibility(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        fixed_rows: Sequence[ThreadSummaryEligibilityRecord],
    ) -> None:
        for fixed in fixed_rows:
            latest = await session.scalar(
                select(ThreadSummaryEligibilityRecord)
                .where(
                    ThreadSummaryEligibilityRecord.workspace_id == workspace_id,
                    ThreadSummaryEligibilityRecord.thread_id == thread_id,
                    ThreadSummaryEligibilityRecord.source_identity == fixed.source_identity,
                )
                .order_by(ThreadSummaryEligibilityRecord.revision.desc())
                .limit(1)
            )
            if latest is None or latest.id != fixed.id:
                raise self._stale_error("The eligibility revision was superseded.")

    @staticmethod
    def _assertions_from_rows(
        rows: Sequence[ThreadSummaryEligibilityRecord],
    ) -> tuple[SummarySourceReviewAssertion, ...]:
        return tuple(
            SummarySourceReviewAssertion(
                source_identity=row.source_identity,
                decision=SummaryReviewDecision(row.decision),
                reviewed_by=row.reviewed_by,
                revision=row.revision,
                reviewer_is_workspace_admin=True,
                review_id=row.id,
            )
            for row in rows
        )

    @staticmethod
    def _build_manifest(
        *,
        manifest: ThreadHistorySourceManifest,
        turns: Sequence[ThreadTurnContext],
        assertions: Sequence[SummarySourceReviewAssertion],
        excerpts: Sequence[SummaryProtectedExcerptRequest],
        workspace_id: UUID,
        thread_id: UUID,
        trigger_run_id: UUID,
    ) -> SummaryProtectedReviewManifest:
        try:
            return build_summary_review_manifest(
                manifest=manifest,
                materialized_turns=turns,
                source_reviews=assertions,
                excerpts=excerpts,
                workspace_id=workspace_id,
                thread_id=thread_id,
                before_run_id=trigger_run_id,
                before_sequence=manifest.scope.before_sequence,
                protected_manifest_complete=True,
            )
        except (SummaryReviewManifestError, TypeError, ValueError):
            raise AgentHubError(
                "SUMMARY_REVIEW_MANIFEST_INVALID",
                "The protected review manifest failed source and excerpt validation.",
                422,
            ) from None

    def _storage_payloads(
        self,
        *,
        manifest: ThreadHistorySourceManifest,
        turns: Sequence[ThreadTurnContext],
        fixed_rows: Sequence[ThreadSummaryEligibilityRecord],
        rebuilt: SummaryProtectedReviewManifest,
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
        turn_by_id = {turn.turn_id: turn for turn in turns}
        row_by_identity = {row.source_identity: row for row in fixed_rows}
        text_by_fact = {fact.fact_ref: fact.text for fact in rebuilt.protected_facts}
        sources: list[dict[str, Any]] = []
        for ordinal, source in enumerate(manifest.selected_sources, start=1):
            turn = turn_by_id[source.turn_id]
            identity = stable_summary_source_identity(source, turn)
            row = row_by_identity[identity]
            sources.append(
                {
                    "ordinal": ordinal,
                    "source_id": source.source_id,
                    "source_identity": identity,
                    "turn_id": source.turn_id,
                    "run_id": source.run_id,
                    "sequence": source.sequence,
                    "source_watermark": source.watermark,
                    "original_ref": source.original_ref,
                    "revision_sha256": source.revision_sha256,
                    "user_input_sha256": source.user_input_sha256,
                    "final_output_sha256": source.final_output_sha256,
                    "artifact_identities": [
                        item.to_manifest() for item in source.artifact_identities
                    ],
                    "review_decision": row.decision,
                    "review_reason": row.reason_code,
                    "eligibility_review_id": row.id,
                    "eligibility_revision": row.revision,
                    "reviewed_by": row.reviewed_by,
                }
            )
        source_by_id = {item["source_id"]: item for item in sources}
        excerpts: list[dict[str, Any]] = []
        for ordinal, excerpt in enumerate(rebuilt.audit.excerpts, start=1):
            text = text_by_fact[excerpt.fact_ref]
            excerpts.append(
                {
                    "ordinal": ordinal,
                    "fact_ref": excerpt.fact_ref,
                    "source_id": excerpt.source_id,
                    "source_identity": excerpt.source_identity,
                    "role": excerpt.role.value,
                    "start_offset": excerpt.start_offset,
                    "end_offset": excerpt.end_offset,
                    "excerpt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
            if source_by_id[excerpt.source_id]["source_identity"] != excerpt.source_identity:
                raise self._stale_error("An excerpt identity differs from its source.")
        return tuple(sources), tuple(excerpts)

    def _stored_sources(
        self,
        *,
        record: SummaryReviewRecord,
        fixed_rows: Sequence[ThreadSummaryEligibilityRecord],
        payloads: Sequence[Mapping[str, Any]],
    ) -> tuple[SummaryReviewSource, ...]:
        row_by_identity = {row.source_identity: row for row in fixed_rows}
        result: list[SummaryReviewSource] = []
        for payload in payloads:
            row = row_by_identity[str(payload["source_identity"])]
            result.append(
                SummaryReviewSource(
                    id=uuid4(),
                    workspace_id=record.workspace_id,
                    thread_id=record.thread_id,
                    review_id=record.id,
                    ordinal=int(payload["ordinal"]),
                    source_id=str(payload["source_id"]),
                    source_identity=str(payload["source_identity"]),
                    turn_id=payload["turn_id"],
                    run_id=payload["run_id"],
                    sequence=int(payload["sequence"]),
                    source_watermark=int(payload["source_watermark"]),
                    original_ref=str(payload["original_ref"]),
                    revision_sha256=str(payload["revision_sha256"]),
                    user_input_sha256=str(payload["user_input_sha256"]),
                    final_output_sha256=str(payload["final_output_sha256"]),
                    artifact_identities=list(payload["artifact_identities"]),
                    review_decision=str(payload["review_decision"]),
                    review_reason=str(payload["review_reason"]),
                    eligibility_review_id=row.id,
                    eligibility_revision=row.revision,
                    reviewed_by=row.reviewed_by,
                )
            )
        return tuple(result)

    @staticmethod
    def _stored_excerpts(
        *,
        record: SummaryReviewRecord,
        payloads: Sequence[Mapping[str, Any]],
    ) -> tuple[SummaryReviewExcerpt, ...]:
        return tuple(
            SummaryReviewExcerpt(
                id=uuid4(),
                workspace_id=record.workspace_id,
                thread_id=record.thread_id,
                review_id=record.id,
                ordinal=int(payload["ordinal"]),
                fact_ref=str(payload["fact_ref"]),
                source_id=str(payload["source_id"]),
                source_identity=str(payload["source_identity"]),
                role=str(payload["role"]),
                start_offset=int(payload["start_offset"]),
                end_offset=int(payload["end_offset"]),
                excerpt_sha256=str(payload["excerpt_sha256"]),
            )
            for payload in payloads
        )

    async def _load_exact_review(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        review_id: UUID,
        revision: int,
    ) -> tuple[
        SummaryReviewRecord,
        tuple[SummaryReviewSource, ...],
        tuple[SummaryReviewExcerpt, ...],
    ]:
        record = await session.scalar(
            select(SummaryReviewRecord).where(
                SummaryReviewRecord.workspace_id == workspace_id,
                SummaryReviewRecord.thread_id == thread_id,
                SummaryReviewRecord.id == review_id,
                SummaryReviewRecord.revision == revision,
            )
        )
        if record is None:
            raise AgentHubError(
                "SUMMARY_REVIEW_NOT_FOUND",
                "The exact protected review revision was not found.",
                404,
            )
        sources = await self._load_source_rows(
            session,
            workspace_id=workspace_id,
            thread_id=thread_id,
            review_id=review_id,
        )
        excerpts = tuple(
            await session.scalars(
                select(SummaryReviewExcerpt)
                .where(
                    SummaryReviewExcerpt.workspace_id == workspace_id,
                    SummaryReviewExcerpt.thread_id == thread_id,
                    SummaryReviewExcerpt.review_id == review_id,
                )
                .order_by(SummaryReviewExcerpt.ordinal)
            )
        )
        if len(sources) != record.source_count or len(excerpts) != record.excerpt_count:
            raise self._stale_error("The persisted review row counts do not match.")
        return record, sources, excerpts

    async def _load_source_rows(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        review_id: UUID,
    ) -> tuple[SummaryReviewSource, ...]:
        return tuple(
            await session.scalars(
                select(SummaryReviewSource)
                .where(
                    SummaryReviewSource.workspace_id == workspace_id,
                    SummaryReviewSource.thread_id == thread_id,
                    SummaryReviewSource.review_id == review_id,
                )
                .order_by(SummaryReviewSource.ordinal)
            )
        )

    async def _load_job_sources(
        self,
        session: AsyncSession,
        job: ThreadSummaryJob,
    ) -> tuple[ThreadSummaryJobSource, ...]:
        return tuple(
            await session.scalars(
                select(ThreadSummaryJobSource)
                .where(
                    ThreadSummaryJobSource.workspace_id == job.workspace_id,
                    ThreadSummaryJobSource.thread_id == job.thread_id,
                    ThreadSummaryJobSource.job_id == job.id,
                )
                .order_by(ThreadSummaryJobSource.ordinal)
            )
        )

    async def _rebuild_and_assert(
        self,
        session: AsyncSession,
        *,
        record: SummaryReviewRecord,
        stored_sources: Sequence[SummaryReviewSource],
        stored_excerpts: Sequence[SummaryReviewExcerpt],
        manifest: ThreadHistorySourceManifest,
        turns: Sequence[ThreadTurnContext],
    ) -> SummaryReviewSnapshot:
        fixed_rows = await self._load_fixed_eligibility(
            session,
            workspace_id=record.workspace_id,
            thread_id=record.thread_id,
            trigger_run_id=record.trigger_run_id,
            eligibility_ids=tuple(source.eligibility_review_id for source in stored_sources),
        )
        await self._assert_latest_eligibility(
            session,
            workspace_id=record.workspace_id,
            thread_id=record.thread_id,
            fixed_rows=fixed_rows,
        )
        assertions = self._assertions_from_rows(fixed_rows)
        requests = tuple(
            SummaryProtectedExcerptRequest(
                fact_ref=excerpt.fact_ref,
                source_id=excerpt.source_id,
                role=excerpt.role,
                start_offset=excerpt.start_offset,
                end_offset=excerpt.end_offset,
            )
            for excerpt in stored_excerpts
        )
        rebuilt = self._build_manifest(
            manifest=manifest,
            turns=turns,
            assertions=assertions,
            excerpts=requests,
            workspace_id=record.workspace_id,
            thread_id=record.thread_id,
            trigger_run_id=record.trigger_run_id,
        )
        if (
            manifest.to_manifest() != record.manifest_json
            or rebuilt.audit.to_record() != record.audit_json
        ):
            raise self._stale_error("The persisted protected review no longer matches.")
        source_payloads, excerpt_payloads = self._storage_payloads(
            manifest=manifest,
            turns=turns,
            fixed_rows=fixed_rows,
            rebuilt=rebuilt,
        )
        expected_hash = self._review_hash(
            workspace_id=record.workspace_id,
            thread_id=record.thread_id,
            trigger_run_id=record.trigger_run_id,
            revision=record.revision,
            review_basis=record.review_basis,
            manifest_json=record.manifest_json,
            audit_json=record.audit_json,
            sources=source_payloads,
            excerpts=excerpt_payloads,
        )
        if expected_hash != record.review_hash:
            raise self._stale_error("The persisted protected review hash differs.")
        text_by_fact = {fact.fact_ref: fact.text for fact in rebuilt.protected_facts}
        digest_by_fact = {
            excerpt.fact_ref: hashlib.sha256(text_by_fact[excerpt.fact_ref].encode("utf-8"))
            .hexdigest()
            for excerpt in rebuilt.audit.excerpts
        }
        if any(
            digest_by_fact.get(excerpt.fact_ref) != excerpt.excerpt_sha256
            for excerpt in stored_excerpts
        ):
            raise self._stale_error("A protected excerpt digest differs.")
        return self._snapshot(
            record=record,
            stored_sources=stored_sources,
            manifest=manifest,
            rebuilt=rebuilt,
            turns=turns,
        )

    @staticmethod
    def _snapshot(
        *,
        record: SummaryReviewRecord,
        stored_sources: Sequence[SummaryReviewSource],
        manifest: ThreadHistorySourceManifest,
        rebuilt: SummaryProtectedReviewManifest,
        turns: Sequence[ThreadTurnContext],
    ) -> SummaryReviewSnapshot:
        turn_by_id = {turn.turn_id: turn for turn in turns}
        summary_sources: list[NewSummarySource] = []
        for source in stored_sources:
            turn = turn_by_id[source.turn_id]
            identity = stable_summary_source_identity(
                next(
                    item
                    for item in manifest.selected_sources
                    if item.turn_id == source.turn_id
                ),
                turn,
            )
            if identity != source.source_identity:
                raise SummaryReviewService._stale_error("A stored source identity changed.")
            summary_sources.append(
                NewSummarySource(
                    source_id=source.source_id,
                    turn_id=source.turn_id,
                    run_id=source.run_id,
                    sequence=source.sequence,
                    original_ref=source.original_ref,
                    source_identity_hash=source.source_identity,
                    revision_sha256=source.revision_sha256,
                    user_input_sha256=source.user_input_sha256,
                    final_output_sha256=source.final_output_sha256,
                    summary_eligibility=source.review_decision,
                    eligibility_reason=source.review_reason,
                    artifact_identities=tuple(
                        dict(item) for item in source.artifact_identities
                    ),
                )
            )
        return SummaryReviewSnapshot(
            record=record,
            manifest=rebuilt,
            summary_sources=tuple(summary_sources),
            materialized_turns=tuple(turns),
        )

    @staticmethod
    def _assert_job_matches_snapshot(
        job: ThreadSummaryJob,
        sources: Sequence[ThreadSummaryJobSource],
        snapshot: SummaryReviewSnapshot,
    ) -> None:
        if (
            job.workspace_id != snapshot.record.workspace_id
            or job.thread_id != snapshot.record.thread_id
            or job.trigger_run_id != snapshot.record.trigger_run_id
            or job.source_watermark != snapshot.record.before_sequence
            or len(sources) != len(snapshot.summary_sources)
        ):
            raise AgentHubError(
                "SUMMARY_REVIEW_JOB_SCOPE_MISMATCH",
                "The summary job does not match the protected review scope.",
                409,
            )
        for stored, reviewed in zip(sources, snapshot.summary_sources, strict=True):
            if (
                stored.source_id != reviewed.source_id
                or stored.turn_id != reviewed.turn_id
                or stored.run_id != reviewed.run_id
                or stored.sequence != reviewed.sequence
                or stored.original_ref != reviewed.original_ref
                or stored.source_identity_hash != reviewed.source_identity_hash
                or stored.revision_sha256 != reviewed.revision_sha256
                or stored.user_input_sha256 != reviewed.user_input_sha256
                or stored.final_output_sha256 != reviewed.final_output_sha256
                or stored.artifact_identities != list(reviewed.artifact_identities)
                or stored.summary_eligibility != reviewed.summary_eligibility
                or stored.eligibility_reason != reviewed.eligibility_reason
            ):
                raise AgentHubError(
                    "SUMMARY_REVIEW_JOB_SOURCE_MISMATCH",
                    "The summary job source manifest differs from the protected review.",
                    409,
                )

    @staticmethod
    def _assert_scope_binding(
        job: ThreadSummaryJob,
        snapshot: SummaryReviewSnapshot,
    ) -> None:
        if not isinstance(job.source_scope, Mapping) or (
            job.source_scope.get(SUMMARY_REVIEW_ID_SCOPE_KEY) != str(snapshot.review_id)
            or job.source_scope.get(SUMMARY_REVIEW_REVISION_SCOPE_KEY) != snapshot.revision
            or job.source_scope.get(SUMMARY_REVIEW_HASH_SCOPE_KEY) != snapshot.review_hash
        ):
            raise AgentHubError(
                "SUMMARY_REVIEW_SCOPE_BINDING_MISSING",
                "The summary job scope must name the exact protected review revision.",
                409,
            )

    @staticmethod
    def _assert_binding_matches_job(
        binding: SummaryJobBinding,
        job: ThreadSummaryJob,
    ) -> None:
        if (
            binding.trigger_run_id != job.trigger_run_id
            or binding.created_by != job.created_by
            or binding.identity_hash != job.identity_hash
            or binding.source_watermark != job.source_watermark
            or dict(binding.source_scope) != job.source_scope
            or binding.source_scope_hash != job.source_scope_hash
            or binding.source_content_hash != job.source_content_hash
            or binding.parent_artifact_id != job.parent_artifact_id
            or binding.parent_revision_hash != job.parent_revision_hash
            or binding.policy_version != job.policy_version
            or binding.prompt_version != job.prompt_version
            or binding.model_spec_hash != job.model_spec_hash
            or binding.permission_snapshot_hash != job.permission_snapshot_hash
            or binding.response_id != job.response_id
            or binding.artifact_id != job.artifact_id
            or binding.model_round != job.model_round
            or binding.checkpoint_id != job.checkpoint_id
            or binding.checkpoint_ns != job.checkpoint_ns
            or (
                binding.checkpoint_model_round_count
                != job.checkpoint_model_round_count
            )
            or (
                binding.checkpoint_binding_sha256
                != job.checkpoint_binding_sha256
            )
        ):
            raise AgentHubError(
                "SUMMARY_REVIEW_BINDING_MISMATCH",
                "The supplied summary binding differs from persisted job state.",
                409,
            )

    @staticmethod
    def _review_hash(
        *,
        workspace_id: UUID,
        thread_id: UUID,
        trigger_run_id: UUID,
        revision: int,
        review_basis: str,
        manifest_json: Mapping[str, Any],
        audit_json: Mapping[str, Any],
        sources: Sequence[Mapping[str, Any]],
        excerpts: Sequence[Mapping[str, Any]],
    ) -> str:
        return canonical_json_hash(
            {
                "version": SUMMARY_REVIEW_STORAGE_VERSION,
                "workspace_id": str(workspace_id),
                "thread_id": str(thread_id),
                "trigger_run_id": str(trigger_run_id),
                "revision": revision,
                "review_basis": review_basis,
                "completeness_status": SUMMARY_REVIEW_COMPLETENESS,
                "protected_manifest_attested": True,
                "reviewer_admin_verified": True,
                "source_manifest": dict(manifest_json),
                "review_audit": dict(audit_json),
                "sources": [SummaryReviewService._json_payload(item) for item in sources],
                "excerpts": [
                    SummaryReviewService._json_payload(item) for item in excerpts
                ],
            }
        )

    @staticmethod
    def _json_payload(value: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in value.items():
            result[key] = str(item) if isinstance(item, UUID) else item
        return result

    @staticmethod
    def _permission_error() -> AgentHubError:
        return AgentHubError("FORBIDDEN", "Workspace access is required.", 403)

    @staticmethod
    def _stale_error(message: str) -> AgentHubError:
        return AgentHubError("SUMMARY_REVIEW_STALE", message, 409)


class SummaryReviewBindingVerifier:
    """Summary verifier adapter with a required trusted context-builder hook."""

    def __init__(
        self,
        service: SummaryReviewService,
        *,
        context_builder: SummaryContextBuilder | None = None,
    ) -> None:
        if not isinstance(service, SummaryReviewService):
            raise TypeError("service must be SummaryReviewService")
        self.service = service
        self.context_builder = context_builder

    async def verify(
        self,
        session: AsyncSession,
        binding: SummaryJobBinding,
    ) -> SummaryTrustDecision:
        try:
            verified = await self.service.verify_job_binding(session, binding)
        except AgentHubError as error:
            return SummaryTrustDecision.denied(error.code)
        except (ThreadHistorySourceIntegrityError, TypeError, ValueError):
            return SummaryTrustDecision.denied("SUMMARY_REVIEW_STALE")
        except Exception:
            return SummaryTrustDecision.unavailable("SUMMARY_REVIEW_STORE_UNAVAILABLE")
        if binding.phase is SummaryVerifierPhase.PUBLICATION:
            return SummaryTrustDecision.verified()
        if self.context_builder is None:
            return SummaryTrustDecision.unavailable(
                "SUMMARY_REVIEW_CONTEXT_BUILDER_REQUIRED"
            )
        try:
            context = self.context_builder(verified.snapshot, verified.job, binding)
        except Exception:
            return SummaryTrustDecision.unavailable(
                "SUMMARY_REVIEW_CONTEXT_BUILD_FAILED"
            )
        if not self._context_matches(verified, context):
            return SummaryTrustDecision.denied("SUMMARY_REVIEW_CONTEXT_MISMATCH")
        return SummaryTrustDecision.verified(context)

    @staticmethod
    def _context_matches(
        verified: VerifiedJobReview,
        context: SummaryInputContext,
    ) -> bool:
        if not isinstance(context, SummaryInputContext):
            return False
        job = verified.job
        snapshot = verified.snapshot
        permission_snapshot_hash = hashlib.sha256(
            context.permission_snapshot.encode("utf-8")
        ).hexdigest()
        if (
            context.workspace_id != str(job.workspace_id)
            or context.thread_id != str(job.thread_id)
            or context.watermark != job.source_watermark
            or context.target_budget_tokens != job.target_budget_tokens
            or (
                context.permission_snapshot != job.permission_snapshot_hash
                and permission_snapshot_hash != job.permission_snapshot_hash
            )
            or context.protected_facts != snapshot.manifest.for_prompt()
            or len(context.sources) != len(snapshot.summary_sources)
            or len(context.sources) != len(snapshot.materialized_turns)
            or context.prefix_first_sequence != snapshot.summary_sources[0].sequence
            or context.prefix_last_sequence != snapshot.summary_sources[-1].sequence
        ):
            return False
        for prompt_source, stored, turn in zip(
            context.sources,
            snapshot.summary_sources,
            snapshot.materialized_turns,
            strict=True,
        ):
            if (
                prompt_source.source_id != stored.source_id
                or prompt_source.workspace_id != str(job.workspace_id)
                or prompt_source.thread_id != str(job.thread_id)
                or prompt_source.identity != stored.source_identity_hash
                or prompt_source.revision != stored.revision_sha256
                or prompt_source.sequence != stored.sequence
                or prompt_source.watermark != job.source_watermark
                or prompt_source.original_ref != stored.original_ref
                or prompt_source.role != "exchange"
                or prompt_source.speaker != "user_and_assistant"
                or prompt_source.content != summary_review_prompt_source_content(turn)
            ):
                return False
        return True


def source_scope_with_review(
    base_scope: Mapping[str, Any],
    snapshot: SummaryReviewSnapshot,
) -> dict[str, Any]:
    """Return a job source scope pinned to this exact persisted review."""

    if not isinstance(snapshot, SummaryReviewSnapshot):
        raise TypeError("snapshot must be SummaryReviewSnapshot")
    scope = dict(base_scope)
    expected = {
        SUMMARY_REVIEW_ID_SCOPE_KEY: str(snapshot.review_id),
        SUMMARY_REVIEW_REVISION_SCOPE_KEY: snapshot.revision,
        SUMMARY_REVIEW_HASH_SCOPE_KEY: snapshot.review_hash,
    }
    for key, value in expected.items():
        if key in scope and scope[key] != value:
            raise ValueError(f"{key} conflicts with the protected review snapshot")
        scope[key] = value
    return scope


__all__ = [
    "CreateSummaryReview",
    "SUMMARY_REVIEW_HASH_SCOPE_KEY",
    "SUMMARY_REVIEW_ID_SCOPE_KEY",
    "SUMMARY_REVIEW_REVISION_SCOPE_KEY",
    "SummaryContextBuilder",
    "SummaryReviewBindingVerifier",
    "SummaryReviewPermissionPolicy",
    "SummaryReviewService",
    "SummaryReviewSnapshot",
    "VerifiedJobReview",
    "summary_review_context_builder",
    "summary_review_prompt_source_content",
    "source_scope_with_review",
]
