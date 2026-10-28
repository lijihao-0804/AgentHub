"""PostgreSQL acceptance tests for durable protected-source reviews."""

from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.adapters.langgraph.checkpoint import checkpoint_thread_id
from packages.agent_runtime.history_checkpoint import (
    history_checkpoint_binding,
    summary_boundary_marker,
)
from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.agent_runtime.summary_models import ThreadSummaryJob
from packages.agent_runtime.summary_service import (
    NewSummaryJob,
    SummaryJobBinding,
    SummaryJobService,
    SummaryTrustStatus,
    SummaryVerifierPhase,
)
from packages.agent_runtime.work_layer import ThreadHistorySourceIdentity
from packages.control_plane.models import (
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.core.config.settings import get_settings
from packages.core.errors.exceptions import AgentHubError
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.models import AgentThread, ThreadTurn
from packages.threads.summary_review_manifest import SummaryProtectedExcerptRequest
from packages.threads.summary_review_models import (
    SUMMARY_REVIEW_BASIS,
    SUMMARY_REVIEW_COMPLETENESS,
    SUMMARY_REVIEW_STORAGE_VERSION,
    SummaryReviewExcerpt,
    SummaryReviewJobBinding,
    SummaryReviewRecord,
    SummaryReviewSource,
)
from packages.threads.summary_review_service import (
    CreateSummaryReview,
    SummaryReviewBindingVerifier,
    SummaryReviewService,
    SummaryReviewSnapshot,
    source_scope_with_review,
    summary_review_context_builder,
)
from tests.integration.test_thread_history_m3 import (
    TEST_DATABASE_URL,
    _add_exchange,
    _create_thread,
    _freeze,
    _seed_workspace,
)
from tests.integration.test_thread_history_m3 import db_factory as db_factory

EXPECTED_DATABASE_URL = (
    "postgresql+psycopg://agenthub:m2_local_test@127.0.0.1:15432/"
    "agenthub_context_policy_m2"
)
REQUIRED_REVISION = "head"
CHECKPOINT_ID = "review-checkpoint"
CHECKPOINT_NS = ""
USER_FACT = "CONFIDENTIAL_USER_SENTINEL: amount=¥1,234.50"
ASSISTANT_FACT = "CONFIDENTIAL_ASSISTANT_SENTINEL: timeout=42 seconds"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set the dedicated protected-review PostgreSQL database URL.",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    assert TEST_DATABASE_URL == EXPECTED_DATABASE_URL, (
        "Protected-review tests may only use the assigned isolated PostgreSQL database."
    )
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), REQUIRED_REVISION)
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


class _ExactCheckpointReader:
    def __init__(self, checkpoint_tuple: SimpleNamespace) -> None:
        self.checkpoint_tuple = checkpoint_tuple

    async def aget_tuple(self, config: dict[str, object]) -> SimpleNamespace:
        assert config == self.checkpoint_tuple.config
        return self.checkpoint_tuple


class _ExactCheckpointAdapter:
    """Test-only reader for the exact checkpoint contract owned by revision 0047."""

    def __init__(self, checkpoint_tuple: SimpleNamespace) -> None:
        self.checkpoint_tuple = checkpoint_tuple

    def config_for_checkpoint(
        self,
        workspace_id: UUID | str,
        run_id: UUID | str,
        *,
        checkpoint_id: str,
        checkpoint_ns: str,
    ) -> dict[str, object]:
        return {
            "configurable": {
                "thread_id": checkpoint_thread_id(workspace_id, run_id),
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    @asynccontextmanager
    async def checkpointer(self):
        yield _ExactCheckpointReader(self.checkpoint_tuple)


@dataclass(frozen=True, slots=True)
class ReviewFixture:
    base: dict[str, UUID]
    thread_id: UUID
    trigger_run_id: UUID
    source: ThreadHistorySourceIdentity
    user_input: str
    final_output: str
    checkpoint_adapter: _ExactCheckpointAdapter


async def _review_fixture(
    factory: async_sessionmaker[AsyncSession],
    *,
    plan_category: str = "OPTIONAL",
) -> ReviewFixture:
    base = await _seed_workspace(factory)
    thread_id = await _create_thread(factory, base)
    user_input = f"Please retain this exact fact. {USER_FACT}"
    final_output = f"The reviewed answer is {ASSISTANT_FACT}."
    await _add_exchange(
        factory,
        base,
        thread_id=thread_id,
        sequence=1,
        user_input=user_input,
        final_output=final_output,
    )
    _, trigger_run_id = await _add_exchange(
        factory,
        base,
        thread_id=thread_id,
        sequence=2,
    )
    await _add_exchange(
        factory,
        base,
        thread_id=thread_id,
        sequence=3,
        status="RUNNING",
    )
    frozen = await _freeze(
        SqlAlchemyThreadContextProvider(factory),
        base,
        thread_id=thread_id,
        run_id=trigger_run_id,
    )
    assert frozen.source_manifest is not None
    async with factory() as session:
        trigger = await session.get(AgentRun, trigger_run_id)
        assert trigger is not None
        version = await session.get(AgentVersion, base["version_id"])
        assert version is not None
        snapshot = {
            "source": frozen.source_manifest.to_manifest(),
            # Synthetic policy fixture: production planning currently marks these
            # sources RECENT/KEEP_EXACT and therefore does not authorize summary.
            "plan": {"decisions": [{
                "group_id": frozen.source_manifest.selected_sources[0].source_id,
                "category": plan_category,
                "eligibility": "ELIGIBLE",
            }]},
        }
        trigger.effective_thread_history_snapshot = snapshot
        history_binding = history_checkpoint_binding(
            workspace_id=base["workspace_id"],
            thread_id=thread_id,
            run_id=trigger_run_id,
            agent_version_id=version.id,
            resolved_spec_hash=version.resolved_spec_hash,
            snapshot=snapshot,
        )
        await session.commit()
    checkpoint_tuple = SimpleNamespace(
        config={
            "configurable": {
                "thread_id": checkpoint_thread_id(base["workspace_id"], trigger_run_id),
                "checkpoint_ns": CHECKPOINT_NS,
                "checkpoint_id": CHECKPOINT_ID,
            }
        },
        checkpoint={
            "id": CHECKPOINT_ID,
            "channel_values": {
                "history_checkpoint_binding": history_binding,
                "model_round_count": 0,
                "summary_boundary": summary_boundary_marker(0),
            },
        },
    )
    return ReviewFixture(
        base=base,
        thread_id=thread_id,
        trigger_run_id=trigger_run_id,
        source=frozen.source_manifest.selected_sources[0],
        user_input=user_input,
        final_output=final_output,
        checkpoint_adapter=_ExactCheckpointAdapter(checkpoint_tuple),
    )


async def _non_admin_actor(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: UUID,
) -> UUID:
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        email = f"review-viewer-{uuid4().hex}@example.test"
        user = User(
            email=email,
            normalized_email=email,
            password_hash="not-used",
        )
        session.add(user)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=workspace.organization_id,
                user_id=user.id,
                role="MEMBER",
            )
        )
        session.add(
            WorkspaceMembership(
                workspace_id=workspace_id,
                user_id=user.id,
                role="DEVELOPER",
            )
        )
        await session.commit()
        return user.id


async def _actor_without_organization_membership(
    factory: async_sessionmaker[AsyncSession],
) -> UUID:
    async with factory() as session:
        email = f"review-outsider-{uuid4().hex}@example.test"
        user = User(
            email=email,
            normalized_email=email,
            password_hash="not-used",
        )
        session.add(user)
        await session.commit()
        return user.id


def _review_command(
    fixture: ReviewFixture,
    *,
    actor_id: UUID | None = None,
) -> CreateSummaryReview:
    user_start = fixture.user_input.index(USER_FACT)
    assistant_start = fixture.final_output.index(ASSISTANT_FACT)
    return CreateSummaryReview(
        workspace_id=fixture.base["workspace_id"],
        thread_id=fixture.thread_id,
        trigger_run_id=fixture.trigger_run_id,
        actor_id=actor_id or fixture.base["user_id"],
        source_ids=(fixture.source.source_id,),
        excerpts=(
            SummaryProtectedExcerptRequest(
                fact_ref="fact-user",
                source_id=fixture.source.source_id,
                role="user",
                start_offset=user_start,
                end_offset=user_start + len(USER_FACT),
            ),
            SummaryProtectedExcerptRequest(
                fact_ref="fact-assistant",
                source_id=fixture.source.source_id,
                role="assistant",
                start_offset=assistant_start,
                end_offset=assistant_start + len(ASSISTANT_FACT),
            ),
        ),
        protected_manifest_complete=True,
    )


def _permission_snapshot(fixture: ReviewFixture) -> str:
    return f"user:{fixture.base['user_id']};permissions:agent_run"


def _permission_hash(snapshot: str) -> str:
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


async def _create_job(
    factory: async_sessionmaker[AsyncSession],
    fixture: ReviewFixture,
    snapshot: SummaryReviewSnapshot,
    *,
    model_round: int,
    sources=None,
) -> ThreadSummaryJob:
    selected_sources = snapshot.summary_sources if sources is None else sources
    base_scope = {
        "kind": "COMPLETED_PREFIX",
        "through_sequence": snapshot.record.before_sequence - 1,
        "before_run_id": str(snapshot.record.trigger_run_id),
    }
    command = NewSummaryJob(
        workspace_id=snapshot.record.workspace_id,
        thread_id=snapshot.record.thread_id,
        trigger_run_id=snapshot.record.trigger_run_id,
        source_watermark=snapshot.record.before_sequence,
        source_scope=source_scope_with_review(base_scope, snapshot),
        policy_spec={"schema_version": 1, "mode": "single_dispatch"},
        policy_version="summary-policy-v1",
        prompt_version="summary_candidate_zh_v1",
        budget_version="summary-budget-v1",
        model_spec={"provider": "offline", "model": "offline-summary-review"},
        tokenizer_id="approx_language_v1",
        tokenizer_version="1",
        target_budget_tokens=1500,
        permission_snapshot_hash=_permission_hash(_permission_snapshot(fixture)),
        created_by=snapshot.record.reviewed_by,
        sources=selected_sources,
        checkpoint_id=CHECKPOINT_ID,
        checkpoint_ns=CHECKPOINT_NS,
        model_round=model_round,
    )
    created = await SummaryJobService(
        factory,
        checkpoint_adapter=fixture.checkpoint_adapter,
    ).create_job(command)
    async with factory() as session:
        job = await session.get(ThreadSummaryJob, created.key.job_id)
        assert job is not None
        return job


def _binding(
    job: ThreadSummaryJob,
    phase: SummaryVerifierPhase,
) -> SummaryJobBinding:
    return SummaryJobBinding(
        workspace_id=job.workspace_id,
        thread_id=job.thread_id,
        job_id=job.id,
        trigger_run_id=job.trigger_run_id,
        created_by=job.created_by,
        identity_hash=job.identity_hash,
        source_watermark=job.source_watermark,
        source_scope=dict(job.source_scope),
        source_scope_hash=job.source_scope_hash,
        source_content_hash=job.source_content_hash,
        parent_artifact_id=job.parent_artifact_id,
        parent_revision_hash=job.parent_revision_hash,
        policy_version=job.policy_version,
        prompt_version=job.prompt_version,
        model_spec_hash=job.model_spec_hash,
        permission_snapshot_hash=job.permission_snapshot_hash,
        response_id=job.response_id,
        artifact_id=job.artifact_id,
        phase=phase,
        model_round=job.model_round,
        checkpoint_id=job.checkpoint_id,
        checkpoint_ns=job.checkpoint_ns,
        checkpoint_model_round_count=job.checkpoint_model_round_count,
        checkpoint_binding_sha256=job.checkpoint_binding_sha256,
    )


def _context_builder(fixture: ReviewFixture):
    return summary_review_context_builder(
        permission_snapshot=_permission_snapshot(fixture),
        task_goal="Continue the reviewed task.",
        completed_actions=(),
        required_references=(),
        source_budget_tokens=2000,
        final_view_budget_tokens=3000,
    )


@pytest.mark.asyncio
async def test_invalid_review_does_not_supersede_existing_eligibility(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _review_fixture(db_factory)
    service = SummaryReviewService(db_factory)
    command = _review_command(fixture)
    original = await service.record_review(command)
    bad_excerpt = replace(command.excerpts[0], end_offset=len(fixture.user_input) + 100)
    with pytest.raises(AgentHubError):
        await service.record_review(replace(command, excerpts=(bad_excerpt,)))
    restored = await service.restore_review(
        workspace_id=original.record.workspace_id,
        thread_id=original.record.thread_id,
        review_id=original.review_id,
        revision=original.revision,
        actor_id=fixture.base["user_id"],
    )
    assert restored.review_id == original.review_id


@pytest.mark.asyncio
async def test_review_cannot_override_frozen_recent_history(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _review_fixture(db_factory, plan_category="RECENT")
    service = SummaryReviewService(db_factory)
    snapshot = await service.record_review(_review_command(fixture))
    job = await _create_job(db_factory, fixture, snapshot, model_round=1)
    await service.attach_review_to_job(
        workspace_id=job.workspace_id, thread_id=job.thread_id, job_id=job.id,
        review_id=snapshot.review_id, revision=snapshot.revision,
    )
    async with db_factory() as session:
        decision = await service.verifier(_context_builder(fixture)).verify(
            session, _binding(job, SummaryVerifierPhase.DISPATCH),
        )
        assert decision.status is SummaryTrustStatus.DENIED


@pytest.mark.asyncio
async def test_only_real_workspace_administrators_can_record_a_review(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _review_fixture(db_factory)
    viewer_id = await _non_admin_actor(
        db_factory,
        fixture.base["workspace_id"],
    )
    with pytest.raises(AgentHubError) as denied:
        await SummaryReviewService(db_factory).record_review(
            _review_command(fixture, actor_id=viewer_id)
        )
    assert denied.value.code == "FORBIDDEN"
    async with db_factory() as session:
        count = await session.scalar(
            select(SummaryReviewRecord.id).where(
                SummaryReviewRecord.workspace_id == fixture.base["workspace_id"],
                SummaryReviewRecord.thread_id == fixture.thread_id,
            )
        )
    assert count is None


@pytest.mark.asyncio
async def test_database_rejects_claimed_admin_without_organization_membership(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _review_fixture(db_factory)
    actor_id = await _actor_without_organization_membership(db_factory)
    async with db_factory() as session:
        trigger = await session.get(AgentRun, fixture.trigger_run_id)
        assert trigger is not None
        assert trigger.effective_thread_history_snapshot is not None
        manifest = trigger.effective_thread_history_snapshot["source"]
        session.add(
            SummaryReviewRecord(
                workspace_id=fixture.base["workspace_id"],
                thread_id=fixture.thread_id,
                trigger_run_id=fixture.trigger_run_id,
                revision=1,
                before_sequence=int(manifest["scope"]["before_sequence"]),
                source_count=int(manifest["selected_source_count"]),
                excerpt_count=0,
                source_manifest_sha256=str(manifest["manifest_sha256"]),
                review_hash="0" * 64,
                review_basis=SUMMARY_REVIEW_BASIS,
                completeness_status=SUMMARY_REVIEW_COMPLETENESS,
                contract_version=SUMMARY_REVIEW_STORAGE_VERSION,
                protected_manifest_attested=True,
                reviewer_admin_verified=True,
                manifest_json=manifest,
                audit_json={},
                reviewed_by=actor_id,
            )
        )
        with pytest.raises(DBAPIError) as rejected:
            await session.commit()
        await session.rollback()
    assert "organization administrator" in str(rejected.value.orig)


@pytest.mark.asyncio
async def test_review_restores_the_exact_revision_without_persisting_text(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _review_fixture(db_factory)
    service = SummaryReviewService(db_factory)
    created = await service.record_review(_review_command(fixture))

    assert [fact.text for fact in created.protected_facts] == [USER_FACT, ASSISTANT_FACT]
    assert USER_FACT not in repr(created)
    restored = await service.restore_review(
        workspace_id=created.record.workspace_id,
        thread_id=created.record.thread_id,
        review_id=created.review_id,
        revision=created.revision,
        actor_id=fixture.base["user_id"],
    )
    assert restored.review_hash == created.review_hash
    assert [fact.text for fact in restored.protected_facts] == [USER_FACT, ASSISTANT_FACT]

    with pytest.raises(AgentHubError) as wrong_revision:
        await service.restore_review(
            workspace_id=created.record.workspace_id,
            thread_id=created.record.thread_id,
            review_id=created.review_id,
            revision=created.revision + 1,
            actor_id=fixture.base["user_id"],
        )
    assert wrong_revision.value.code == "SUMMARY_REVIEW_NOT_FOUND"
    with pytest.raises(AgentHubError) as wrong_tenant:
        await service.restore_review(
            workspace_id=uuid4(),
            thread_id=created.record.thread_id,
            review_id=created.review_id,
            revision=created.revision,
            actor_id=fixture.base["user_id"],
        )
    assert wrong_tenant.value.code == "SUMMARY_REVIEW_NOT_FOUND"

    async with db_factory() as session:
        for table_name in (
            "summary_review_records",
            "summary_review_sources",
            "summary_review_excerpts",
            "summary_review_job_bindings",
        ):
            persisted = await session.scalar(
                text(
                    f"SELECT coalesce(jsonb_agg(to_jsonb(row_value)), '[]'::jsonb)::text "
                    f"FROM {table_name} AS row_value "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": fixture.base["workspace_id"]},
            )
            assert USER_FACT not in persisted
            assert ASSISTANT_FACT not in persisted


@pytest.mark.asyncio
async def test_revocation_and_source_mutation_invalidate_restore(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    revoked_fixture = await _review_fixture(db_factory)
    service = SummaryReviewService(db_factory)
    revoked = await service.record_review(_review_command(revoked_fixture))
    async with db_factory() as session:
        workspace = await session.get(Workspace, revoked_fixture.base["workspace_id"])
        assert workspace is not None
        membership = await session.get(
            OrganizationMembership,
            (workspace.organization_id, revoked_fixture.base["user_id"]),
        )
        assert membership is not None
        membership.role = "MEMBER"
        await session.commit()
    with pytest.raises(AgentHubError) as permission_revoked:
        await service.restore_review(
            workspace_id=revoked.record.workspace_id,
            thread_id=revoked.record.thread_id,
            review_id=revoked.review_id,
            revision=revoked.revision,
            actor_id=revoked_fixture.base["user_id"],
        )
    assert permission_revoked.value.code == "SUMMARY_REVIEW_PERMISSION_REVOKED"

    mutated_fixture = await _review_fixture(db_factory)
    mutated = await service.record_review(_review_command(mutated_fixture))
    async with db_factory() as session:
        await session.execute(
            update(ThreadTurn)
            .where(
                ThreadTurn.workspace_id == mutated_fixture.base["workspace_id"],
                ThreadTurn.thread_id == mutated_fixture.thread_id,
                ThreadTurn.id == mutated_fixture.source.turn_id,
            )
            .values(user_input="changed after review")
        )
        await session.commit()
    with pytest.raises(AgentHubError) as stale:
        await service.restore_review(
            workspace_id=mutated.record.workspace_id,
            thread_id=mutated.record.thread_id,
            review_id=mutated.review_id,
            revision=mutated.revision,
            actor_id=mutated_fixture.base["user_id"],
        )
    assert stale.value.code == "SUMMARY_REVIEW_STALE"


@pytest.mark.asyncio
async def test_concurrent_reviews_append_and_restore_never_selects_latest(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _review_fixture(db_factory)
    service = SummaryReviewService(db_factory)
    first, second = await asyncio.gather(
        service.record_review(_review_command(fixture)),
        service.record_review(_review_command(fixture)),
    )
    assert sorted((first.revision, second.revision)) == [1, 2]
    old, newest = sorted((first, second), key=lambda snapshot: snapshot.revision)
    with pytest.raises(AgentHubError) as old_revision:
        await service.restore_review(
            workspace_id=old.record.workspace_id,
            thread_id=old.record.thread_id,
            review_id=old.review_id,
            revision=old.revision,
            actor_id=fixture.base["user_id"],
        )
    assert old_revision.value.code == "SUMMARY_REVIEW_STALE"
    restored = await service.restore_review(
        workspace_id=newest.record.workspace_id,
        thread_id=newest.record.thread_id,
        review_id=newest.review_id,
        revision=newest.revision,
        actor_id=fixture.base["user_id"],
    )
    assert restored.review_id == newest.review_id

    async with db_factory() as session:
        revisions = tuple(
            await session.scalars(
                select(SummaryReviewRecord.revision)
                .where(
                    SummaryReviewRecord.workspace_id == fixture.base["workspace_id"],
                    SummaryReviewRecord.thread_id == fixture.thread_id,
                )
                .order_by(SummaryReviewRecord.revision)
            )
        )
        eligibility_revisions = tuple(
            await session.scalars(
                select(SummaryReviewSource.eligibility_revision)
                .join(
                    SummaryReviewRecord,
                    (SummaryReviewSource.workspace_id == SummaryReviewRecord.workspace_id)
                    & (SummaryReviewSource.thread_id == SummaryReviewRecord.thread_id)
                    & (SummaryReviewSource.review_id == SummaryReviewRecord.id),
                )
                .where(
                    SummaryReviewRecord.workspace_id == fixture.base["workspace_id"],
                    SummaryReviewRecord.thread_id == fixture.thread_id,
                )
                .order_by(SummaryReviewRecord.revision)
            )
        )
    assert revisions == (1, 2)
    assert eligibility_revisions == (1, 2)


@pytest.mark.asyncio
async def test_retained_reviews_and_job_bindings_reject_mutation(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _review_fixture(db_factory)
    service = SummaryReviewService(db_factory)
    snapshot = await service.record_review(_review_command(fixture))
    job = await _create_job(
        db_factory,
        fixture,
        snapshot,
        model_round=1,
    )
    binding = await service.attach_review_to_job(
        workspace_id=snapshot.record.workspace_id,
        thread_id=snapshot.record.thread_id,
        job_id=job.id,
        review_id=snapshot.review_id,
        revision=snapshot.revision,
    )
    async with db_factory() as session:
        source = await session.scalar(
            select(SummaryReviewSource).where(
                SummaryReviewSource.review_id == snapshot.review_id
            )
        )
        excerpt = await session.scalar(
            select(SummaryReviewExcerpt).where(
                SummaryReviewExcerpt.review_id == snapshot.review_id
            )
        )
    assert source is not None and excerpt is not None

    statements = (
        update(SummaryReviewRecord)
        .where(SummaryReviewRecord.id == snapshot.review_id)
        .values(review_hash="0" * 64),
        update(SummaryReviewSource)
        .where(SummaryReviewSource.id == source.id)
        .values(original_ref="changed"),
        delete(SummaryReviewExcerpt).where(SummaryReviewExcerpt.id == excerpt.id),
        delete(SummaryReviewJobBinding).where(SummaryReviewJobBinding.id == binding.id),
        text(
            "TRUNCATE TABLE summary_review_job_bindings, "
            "summary_review_excerpts, summary_review_sources, "
            "summary_review_records"
        ),
    )
    for statement in statements:
        async with db_factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(statement)
            await session.rollback()


@pytest.mark.asyncio
async def test_job_binding_and_all_verifier_phases_recheck_fixed_state(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _review_fixture(db_factory)
    service = SummaryReviewService(db_factory)
    snapshot = await service.record_review(_review_command(fixture))
    job = await _create_job(
        db_factory,
        fixture,
        snapshot,
        model_round=1,
    )
    await service.attach_review_to_job(
        workspace_id=snapshot.record.workspace_id,
        thread_id=snapshot.record.thread_id,
        job_id=job.id,
        review_id=snapshot.review_id,
        revision=snapshot.revision,
    )
    builder = _context_builder(fixture)
    verifier = SummaryReviewBindingVerifier(service, context_builder=builder)

    async with db_factory() as session:
        dispatch = await verifier.verify(
            session,
            _binding(job, SummaryVerifierPhase.DISPATCH),
        )
        await session.rollback()
    assert dispatch.status is SummaryTrustStatus.VERIFIED
    assert dispatch.validation_context is not None
    assert USER_FACT in dispatch.validation_context.sources[0].content
    assert ASSISTANT_FACT in dispatch.validation_context.sources[0].content
    assert dispatch.validation_context.sources[0].role == "exchange"
    assert dispatch.validation_context.sources[0].speaker == "user_and_assistant"

    # Publication already holds this row. Verification must reuse its session
    # rather than opening a second transaction that waits on our own lock.
    async with db_factory() as session:
        await session.scalar(
            select(AgentThread)
            .where(AgentThread.id == fixture.thread_id)
            .with_for_update()
        )
        locked_decision = await asyncio.wait_for(
            verifier.verify(session, _binding(job, SummaryVerifierPhase.PUBLICATION)),
            timeout=3,
        )
        assert locked_decision.status is SummaryTrustStatus.VERIFIED
        await session.rollback()

    response_context = None
    for phase in (
        SummaryVerifierPhase.RESPONSE_VALIDATION,
        SummaryVerifierPhase.PUBLICATION,
    ):
        async with db_factory() as session:
            decision = await verifier.verify(session, _binding(job, phase))
            await session.rollback()
        assert decision.status is SummaryTrustStatus.VERIFIED
        if phase is SummaryVerifierPhase.RESPONSE_VALIDATION:
            response_context = decision.validation_context
    assert response_context is not None
    assert USER_FACT in response_context.sources[0].content
    assert ASSISTANT_FACT in response_context.sources[0].content

    tampered = replace(
        _binding(job, SummaryVerifierPhase.RESPONSE_VALIDATION),
        checkpoint_binding_sha256="0" * 64,
    )
    async with db_factory() as session:
        tampered_decision = await verifier.verify(session, tampered)
        await session.rollback()
    assert tampered_decision.status is SummaryTrustStatus.DENIED
    assert tampered_decision.reason_codes == ("SUMMARY_REVIEW_BINDING_MISMATCH",)

    def bad_context_builder(snapshot_value, job_value, binding_value):
        return replace(
            builder(snapshot_value, job_value, binding_value),
            protected_facts=(),
        )

    bad_verifier = SummaryReviewBindingVerifier(
        service,
        context_builder=bad_context_builder,
    )
    async with db_factory() as session:
        bad_context = await bad_verifier.verify(
            session,
            _binding(job, SummaryVerifierPhase.DISPATCH),
        )
        await session.rollback()
    assert bad_context.status is SummaryTrustStatus.DENIED
    assert bad_context.reason_codes == ("SUMMARY_REVIEW_CONTEXT_MISMATCH",)

    async with db_factory() as session:
        workspace = await session.get(Workspace, fixture.base["workspace_id"])
        assert workspace is not None
        membership = await session.get(
            OrganizationMembership,
            (workspace.organization_id, fixture.base["user_id"]),
        )
        assert membership is not None
        membership.role = "MEMBER"
        await session.commit()
    for phase in (
        SummaryVerifierPhase.RESPONSE_VALIDATION,
        SummaryVerifierPhase.PUBLICATION,
    ):
        async with db_factory() as session:
            revoked = await verifier.verify(
                session,
                _binding(job, phase),
            )
            await session.rollback()
        assert revoked.status is SummaryTrustStatus.DENIED
        assert revoked.reason_codes == ("SUMMARY_REVIEW_PERMISSION_REVOKED",)


@pytest.mark.asyncio
async def test_job_with_different_source_manifest_cannot_bind(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _review_fixture(db_factory)
    service = SummaryReviewService(db_factory)
    snapshot = await service.record_review(_review_command(fixture))
    mismatched_sources = (
        replace(
            snapshot.summary_sources[0],
            source_identity_hash="1" * 64,
        ),
    )
    job = await _create_job(
        db_factory,
        fixture,
        snapshot,
        model_round=1,
        sources=mismatched_sources,
    )
    with pytest.raises(AgentHubError) as mismatch:
        await service.attach_review_to_job(
            workspace_id=snapshot.record.workspace_id,
            thread_id=snapshot.record.thread_id,
            job_id=job.id,
            review_id=snapshot.review_id,
            revision=snapshot.revision,
        )
    assert mismatch.value.code == "SUMMARY_REVIEW_JOB_SOURCE_MISMATCH"
