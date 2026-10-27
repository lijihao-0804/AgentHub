"""Isolated PostgreSQL checks for audited Thread source eligibility."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from packages.agent_runtime.models import AgentRun
from packages.control_plane.models import OrganizationMembership, Workspace
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.models import AgentThread, ThreadTurn
from packages.threads.service import ThreadService
from packages.threads.summary_eligibility import (
    EligibilityDecision,
    SummaryEligibilityService,
    ThreadSummaryEligibilityRecord,
)
from tests.integration.test_thread_history_m3 import (
    TEST_DATABASE_URL,
    _add_artifact,
    _add_exchange,
    _create_thread,
    _freeze,
    _seed_workspace,
)
from tests.integration.test_thread_history_m3 import db_factory as db_factory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Requires isolated PostgreSQL"),
]


async def _fixture(factory, *, artifact=False):
    base = await _seed_workspace(factory)
    thread = await _create_thread(factory, base)
    turn, source_run = await _add_exchange(factory, base, thread_id=thread, sequence=1)
    if artifact:
        await _add_artifact(
            factory,
            base,
            thread_id=thread,
            run_id=source_run,
            title="retained artifact",
            created_at=datetime.now(UTC),
        )
    _, trigger_run = await _add_exchange(factory, base, thread_id=thread, sequence=2)
    _, later_run = await _add_exchange(
        factory, base, thread_id=thread, sequence=3, status="RUNNING"
    )
    provider = SqlAlchemyThreadContextProvider(factory)
    first = await _freeze(provider, base, thread_id=thread, run_id=trigger_run)
    later = await _freeze(provider, base, thread_id=thread, run_id=later_run)
    async with factory() as session:
        for run_id, view in ((trigger_run, first), (later_run, later)):
            run = await session.get(AgentRun, run_id)
            assert run is not None and view.source_manifest is not None
            run.effective_thread_history_snapshot = {"source": view.source_manifest.to_manifest()}
        await session.commit()
    return (
        base,
        thread,
        turn,
        source_run,
        trigger_run,
        later_run,
        first.source_manifest.selected_sources[0],
    )


def _args(base, thread, trigger_run, source):
    return dict(
        workspace_id=base["workspace_id"],
        thread_id=thread,
        trigger_run_id=trigger_run,
        source_id=source.source_id,
        actor_id=base["user_id"],
    )


@pytest.mark.asyncio
async def test_review_is_audited_and_reused_across_watermarks_without_reclassifying(db_factory):
    base, thread, _, _, first, later, source = await _fixture(db_factory)
    service = SummaryEligibilityService(db_factory)
    args = _args(base, thread, first, source)
    assert (await service.check(**args)).decision is EligibilityDecision.UNKNOWN
    created = await service.review(**args, decision=EligibilityDecision.ELIGIBLE)
    assert created.decision is EligibilityDecision.ELIGIBLE
    later_args = _args(base, thread, later, source)
    assert (await service.check(**later_args)).review_id == created.review_id
    await service.review(**later_args, decision=EligibilityDecision.INELIGIBLE)
    assert (await service.check(**args)).decision is EligibilityDecision.INELIGIBLE
    async with db_factory() as session:
        rows = tuple(
            await session.scalars(
                select(ThreadSummaryEligibilityRecord)
                .where(ThreadSummaryEligibilityRecord.thread_id == thread)
                .order_by(ThreadSummaryEligibilityRecord.revision)
            )
        )
    assert [row.revision for row in rows] == [1, 2]


@pytest.mark.asyncio
async def test_reviewer_demotion_revokes_eligibility_and_prevents_new_review(db_factory):
    base, thread, _, _, first, _, source = await _fixture(db_factory)
    service = SummaryEligibilityService(db_factory)
    args = _args(base, thread, first, source)
    await service.review(**args, decision=EligibilityDecision.ELIGIBLE)
    async with db_factory() as session:
        workspace = await session.get(Workspace, base["workspace_id"])
        membership = await session.get(
            OrganizationMembership, (workspace.organization_id, base["user_id"])
        )
        membership.role = "MEMBER"
        await session.commit()
    assert (await service.check(**args)).reason == "REVIEWER_REVOKED"
    with pytest.raises(AgentHubError) as denied:
        await service.review(**args, decision=EligibilityDecision.ELIGIBLE)
    assert denied.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_changed_source_or_wrong_workspace_never_reuses_a_review(db_factory):
    base, thread, turn_id, _, first, _, source = await _fixture(db_factory)
    service = SummaryEligibilityService(db_factory)
    args = _args(base, thread, first, source)
    await service.review(**args, decision=EligibilityDecision.ELIGIBLE)
    async with db_factory() as session:
        await session.execute(
            update(ThreadTurn).where(ThreadTurn.id == turn_id).values(user_input="changed")
        )
        await session.commit()
    assert (await service.check(**args)).decision is EligibilityDecision.UNKNOWN
    with pytest.raises(AgentHubError):
        await service.check(**{**args, "workspace_id": uuid4()})


@pytest.mark.asyncio
async def test_artifacts_and_tool_evidence_remain_exact(db_factory):
    base, thread, _, _, first, _, source = await _fixture(db_factory, artifact=True)
    service = SummaryEligibilityService(db_factory)
    args = _args(base, thread, first, source)
    with pytest.raises(AgentHubError) as blocked:
        await service.review(**args, decision=EligibilityDecision.ELIGIBLE)
    assert blocked.value.code == "SUMMARY_SOURCE_REQUIRES_EXACT"
    assert (
        await service.review(**args, decision=EligibilityDecision.INELIGIBLE)
    ).decision is EligibilityDecision.INELIGIBLE


@pytest.mark.asyncio
async def test_concurrent_reviews_append_and_database_rejects_mutation(db_factory):
    base, thread, _, _, first, _, source = await _fixture(db_factory)
    service = SummaryEligibilityService(db_factory)
    args = _args(base, thread, first, source)
    first_review, second_review = await asyncio.gather(
        service.review(**args, decision=EligibilityDecision.ELIGIBLE),
        service.review(**args, decision=EligibilityDecision.INELIGIBLE),
    )
    assert first_review.review_id != second_review.review_id
    async with db_factory() as session:
        rows = tuple(
            await session.scalars(
                select(ThreadSummaryEligibilityRecord)
                .where(ThreadSummaryEligibilityRecord.thread_id == thread)
                .order_by(ThreadSummaryEligibilityRecord.revision)
            )
        )
    assert [row.revision for row in rows] == [1, 2]
    for statement in (
        update(ThreadSummaryEligibilityRecord)
        .where(ThreadSummaryEligibilityRecord.id == rows[0].id)
        .values(reason_code="KEEP_EXACT"),
        delete(ThreadSummaryEligibilityRecord).where(
            ThreadSummaryEligibilityRecord.id == rows[0].id
        ),
    ):
        async with db_factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(statement)
            await session.rollback()


@pytest.mark.asyncio
async def test_thread_deletion_reports_retained_review_and_preserves_other_deletions(db_factory):
    base, thread, _, _, first, _, source = await _fixture(db_factory)
    await SummaryEligibilityService(db_factory).review(
        **_args(base, thread, first, source), decision=EligibilityDecision.ELIGIBLE
    )
    context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="test", trace_id="test", user_id=str(base["user_id"])
            ),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(base["workspace_id"]),
        permissions=frozenset({"agent_run"}),
    )
    service = ThreadService(db_factory)
    with pytest.raises(AgentHubError) as blocked:
        await service.delete_thread(context, thread)
    assert blocked.value.code == "SUMMARY_EVIDENCE_RETAINED"
    assert blocked.value.status_code == 409
    other_thread = await _create_thread(db_factory, base)
    await service.delete_thread(context, other_thread)
    async with db_factory() as session:
        assert await session.get(AgentThread, thread) is not None
        assert await session.get(AgentThread, other_thread) is None
