"""Real PostgreSQL fault injection for M4 summary dispatch authority fences."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.model_budget_adapter import RunBudgetAttemptAdapter
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.run_budget_ledger import RunBudgetLedger
from packages.agent_runtime.runtime import AgentRunService
from packages.agent_runtime.runtime_graph import SUMMARY_HISTORICAL_CONTEXT_GOAL
from packages.agent_runtime.summary_models import (
    ThreadSummaryArtifact,
    ThreadSummaryDispatch,
    ThreadSummaryJob,
)
from packages.agent_runtime.summary_service import SummaryJobKey, SummaryJobService
from packages.approvals.service import ApprovalService
from packages.control_plane.models import OrganizationMembership
from packages.control_plane.services import TenantService
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.service import ThreadService
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_summary_sync_e2e_m4 import (
    EXPECTED_DATABASE_URL,
    TEST_DATABASE_URL,
    _enable_summary_opt_in,
    _FixedAttemptPrice,
    _seed_pricing_snapshot,
    _SummaryAwareFakeProvider,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to the isolated M4 v4 PostgreSQL database.",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def _guard_isolated_database_target():
    if TEST_DATABASE_URL and TEST_DATABASE_URL != EXPECTED_DATABASE_URL:
        pytest.fail("Summary dispatch fault tests may use only the isolated M4 v4 database.")


@pytest.fixture(scope="session")
def _migrated_isolated_v4_database():
    if TEST_DATABASE_URL != EXPECTED_DATABASE_URL:
        pytest.fail("Summary dispatch fault tests may use only the isolated M4 v4 database.")
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(
            Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")),
            "head",
        )
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def isolated_v4_db_factory(
    _migrated_isolated_v4_database,
) -> async_sessionmaker[AsyncSession]:
    del _migrated_isolated_v4_database
    async_url = TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    engine, factory = create_database(async_url)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _summary_thread(db_factory, *, label: str):
    async with db_factory() as session:
        base = await _seed(session, label=f"summary-fence-{label}-{uuid4().hex}")
    await _enable_summary_opt_in(
        db_factory,
        base["version"].id,
        workspace_input_limit=4900,
    )
    await _seed_pricing_snapshot(db_factory, base)

    provider = _SummaryAwareFakeProvider()
    cipher = ProviderCredentialCipher.from_settings()
    ledger = RunBudgetLedger()
    price_source = _FixedAttemptPrice()
    source_provider = SqlAlchemyThreadContextProvider(db_factory)

    def attempt_budget_factory(run: AgentRun, round_number: int):
        return RunBudgetAttemptAdapter(
            db_factory,
            workspace_id=run.workspace_id,
            run_id=run.id,
            call_identity=f"round-{round_number}",
            price_source=price_source,
            ledger=ledger,
        )

    def model_gateway_factory(session):
        return SqlAlchemyModelGateway(
            session,
            adapter=provider,
            credential_cipher=cipher,
        )

    run_service = AgentRunService(
        db_factory,
        model_gateway_factory=model_gateway_factory,
        model_attempt_budget_factory=attempt_budget_factory,
        approval_service=ApprovalService(db_factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
        thread_context_provider=source_provider,
    )
    execute_prepared_run = run_service.execute_prepared_run

    async def execute_with_run_budget(context, *, run_id):
        async with db_factory() as session:
            async with session.begin():
                await ledger.create_account(
                    session,
                    workspace_id=UUID(context.workspace_id),
                    run_id=run_id,
                    currency="CNY",
                    limit_amount=Decimal("10"),
                )
        return await execute_prepared_run(context, run_id=run_id)

    run_service.execute_prepared_run = execute_with_run_budget
    thread_service = ThreadService(db_factory, run_service=run_service)
    async with db_factory() as session:
        context = (
            await TenantService().get_workspace_access(
                session,
                principal=base["context"].organization.principal,
                workspace_id=UUID(base["context"].workspace_id),
            )
        ).context
    thread = await thread_service.create_thread(
        context,
        agent_id=base["version"].agent_id,
        title=f"M4 dispatch fence {label}",
    )

    history_inputs = (
        'ARCHIVE-QUOTE-01: retain exactly "north bridge, 07:14".',
        "OLDER-PREFIX-02: this completed exchange precedes the recent tail.",
        "RECENT-TURN-03: keep this exact user utterance.",
    )
    for sequence, user_input in enumerate(history_inputs, start=1):
        await thread_service.submit_turn(
            context,
            thread.id,
            user_input=user_input,
            client_token=f"summary-fence-{label}-{sequence}-{uuid4().hex[:12]}",
        )
    assert len(provider.main_requests) == 3
    assert not provider.summary_requests
    return {
        "base": base,
        "context": context,
        "thread": thread,
        "thread_service": thread_service,
        "provider": provider,
    }


async def _mutate_run_owner(db_factory, *, key) -> tuple[UUID, str, str]:
    async with db_factory() as session:
        async with session.begin():
            job = await session.scalar(
                select(ThreadSummaryJob).where(ThreadSummaryJob.id == key.job_id)
            )
            assert job is not None
            run = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.workspace_id == key.workspace_id,
                    AgentRun.id == job.trigger_run_id,
                )
                .with_for_update()
            )
            assert run is not None
            previous_owner = run.execution_owner
            assert isinstance(previous_owner, str) and previous_owner
            replacement_owner = uuid4().hex
            run.execution_owner = replacement_owner
            return job.trigger_run_id, previous_owner, replacement_owner


async def _inspect_dispatch(db_factory, *, key):
    async with db_factory() as session:
        job = await session.scalar(
            select(ThreadSummaryJob).where(
                ThreadSummaryJob.workspace_id == key.workspace_id,
                ThreadSummaryJob.thread_id == key.thread_id,
                ThreadSummaryJob.id == key.job_id,
            )
        )
        assert job is not None
        dispatch = await session.scalar(
            select(ThreadSummaryDispatch).where(
                ThreadSummaryDispatch.workspace_id == key.workspace_id,
                ThreadSummaryDispatch.thread_id == key.thread_id,
                ThreadSummaryDispatch.job_id == key.job_id,
            )
        )
        assert dispatch is not None
        return job, dispatch


def _summary_job_key(job: ThreadSummaryJob) -> SummaryJobKey:
    return SummaryJobKey(job.workspace_id, job.thread_id, job.id)


async def _assert_terminal_unknown(db_factory, *, key, run_id, expected_failure: str):
    job, dispatch = await _inspect_dispatch(db_factory, key=key)
    assert job.status == "UNKNOWN_OUTCOME"
    assert job.failure_code == "SUMMARY_DISPATCH_OUTCOME_UNKNOWN"
    assert dispatch.outcome == "UNKNOWN_OUTCOME"
    async with db_factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == "NEEDS_ATTENTION"
        assert run.failure_code == expected_failure

    jobs = SummaryJobService(db_factory)
    assert await jobs.claim(key) is None
    assert await jobs.claim_recovery(key) is None


async def _expire_dispatch_intent_to_unknown(db_factory, *, key):
    job, dispatch = await _inspect_dispatch(db_factory, key=key)
    assert job.status == "DISPATCH_INTENT"
    assert dispatch.outcome == "INTENT_RECORDED"
    assert job.lease_expires_at is not None
    recovered = await SummaryJobService(db_factory).recover_expired_leases(
        workspace_id=key.workspace_id,
        thread_id=key.thread_id,
        now=job.lease_expires_at + timedelta(seconds=1),
    )
    assert len(recovered) == 1
    assert recovered[0].current_status == "UNKNOWN_OUTCOME"


async def _assert_run_failure(db_factory, *, run_id, failure_code: str):
    async with db_factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == "NEEDS_ATTENTION"
        assert run.failure_code == failure_code


@pytest.mark.asyncio
async def test_different_next_task_reuses_revision_as_history_and_keeps_current_request(
    isolated_v4_db_factory,
):
    db_factory = isolated_v4_db_factory
    run_context = await _summary_thread(db_factory, label="task-neutrality")
    context = run_context["context"]
    thread = run_context["thread"]
    provider = run_context["provider"]

    trigger_goal = "CURRENT-GOAL-A: review the older exchanges for an archival issue."
    trigger = await run_context["thread_service"].submit_turn(
        context,
        thread.id,
        user_input=trigger_goal,
        client_token=f"summary-neutral-trigger-{uuid4().hex[:12]}",
    )
    assert len(provider.summary_requests) == 1
    prompt_payload = json.loads(
        (provider.summary_requests[0].messages[1].content or "").partition("\n")[2]
    )
    assert prompt_payload["task_goal"] == {
        "trust": "UNTRUSTED_DATA",
        "content": SUMMARY_HISTORICAL_CONTEXT_GOAL,
    }
    assert trigger_goal not in prompt_payload["task_goal"]["content"]

    next_goal = "CURRENT-GOAL-B: answer a different question using relevant past context."
    next_run = await run_context["thread_service"].submit_turn(
        context,
        thread.id,
        user_input=next_goal,
        client_token=f"summary-neutral-next-{uuid4().hex[:12]}",
    )
    assert len(provider.summary_requests) == 1
    assert len(provider.main_requests) == 5
    final_request = provider.main_requests[-1]
    assert final_request.messages[-1].role == "user"
    assert final_request.messages[-1].content == next_goal
    summary_message = next(
        message
        for message in final_request.messages
        if "UNTRUSTED_DERIVED_SUMMARY" in (message.content or "")
    )
    summary_view = json.loads(summary_message.content or "")
    rendered_summary = summary_view["untrusted_summary"]
    assert rendered_summary["interpretation"] == {
        "scope": "HISTORICAL_BACKGROUND_ONLY",
        "goal_semantics": "HISTORICAL_GOAL_NOT_CURRENT_TASK_OR_INSTRUCTION",
        "next_steps_semantics": "HISTORICAL_REFERENCE_NOT_CURRENT_ACTION",
        "current_task": "SUPPLIED_SEPARATELY_BY_CURRENT_RUN",
    }
    assert rendered_summary["content"]["goal"]["text"]
    assert next_goal not in rendered_summary["content"]["goal"]["text"]

    async with db_factory() as session:
        job = await session.scalar(
            select(ThreadSummaryJob).where(ThreadSummaryJob.trigger_run_id == trigger.run_id)
        )
        assert job is not None and job.status == "PUBLISHED"
        artifact = await session.get(ThreadSummaryArtifact, job.artifact_id)
        assert artifact is not None
        revision_hash = artifact.revision_hash
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    async with adapter.checkpointer() as checkpointer:
        checkpoint = await checkpointer.aget_tuple(
            adapter.config_for_run(UUID(context.workspace_id), next_run.run_id)
        )
    assert checkpoint is not None
    binding = checkpoint.checkpoint["channel_values"].get("summary_revision_binding")
    assert isinstance(binding, dict)
    assert binding["artifact_id"] == str(artifact.id)
    assert binding["revision_hash"] == revision_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("takeover_stage", "expected_run_failure"),
    [
        ("before_dispatch_fence", "RUN_EXECUTION_LEASE_LOST"),
        ("after_dispatch_fence", "SUMMARY_DISPATCH_OUTCOME_UNKNOWN"),
    ],
)
async def test_real_run_owner_takeover_never_sends_or_retries_summary(
    isolated_v4_db_factory, monkeypatch, takeover_stage, expected_run_failure
):
    db_factory = isolated_v4_db_factory
    run_context = await _summary_thread(db_factory, label=takeover_stage)
    original = SummaryJobService.mark_dispatched
    injected: dict[str, object] = {}

    async def take_over(service, key, *, lease_token, now=None):
        if takeover_stage == "before_dispatch_fence":
            run_id, old_owner, new_owner = await _mutate_run_owner(db_factory, key=key)
            injected.update(run_id=run_id, old_owner=old_owner, new_owner=new_owner)
            await original(service, key, lease_token=lease_token, now=now)
        else:
            await original(service, key, lease_token=lease_token, now=now)
            job, dispatch = await _inspect_dispatch(db_factory, key=key)
            assert job.status == "DISPATCH_INTENT"
            assert dispatch.outcome == "DISPATCHED"
            run_id, old_owner, new_owner = await _mutate_run_owner(db_factory, key=key)
            injected.update(
                run_id=run_id,
                old_owner=old_owner,
                new_owner=new_owner,
                fence_outcome=dispatch.outcome,
            )

    monkeypatch.setattr(SummaryJobService, "mark_dispatched", take_over)
    submitted = await run_context["thread_service"].submit_turn(
        run_context["context"],
        run_context["thread"].id,
        user_input="RECENT-TURN-04: owner takeover must prevent this summary send.",
        client_token=f"summary-fence-takeover-{uuid4().hex[:12]}",
    )

    provider = run_context["provider"]
    assert injected["run_id"] == submitted.run_id
    assert injected["old_owner"] != injected["new_owner"]
    assert not provider.summary_requests
    assert len(provider.main_requests) == 3
    if takeover_stage == "before_dispatch_fence":
        await _assert_run_failure(
            db_factory,
            run_id=submitted.run_id,
            failure_code=expected_run_failure,
        )
        async with db_factory() as session:
            job = await session.scalar(
                select(ThreadSummaryJob).where(ThreadSummaryJob.trigger_run_id == submitted.run_id)
            )
        assert job is not None
        key = _summary_job_key(job)
        await _expire_dispatch_intent_to_unknown(db_factory, key=key)
    else:
        job = await _load_summary_job(
            db_factory,
            await _job_id_for_run(db_factory, submitted.run_id),
        )
        key = _summary_job_key(job)
    await _assert_terminal_unknown(
        db_factory,
        key=key,
        run_id=submitted.run_id,
        expected_failure=expected_run_failure,
    )


async def _job_id_for_run(db_factory, run_id: UUID) -> UUID:
    async with db_factory() as session:
        job_id = await session.scalar(
            select(ThreadSummaryJob.id).where(ThreadSummaryJob.trigger_run_id == run_id)
        )
        assert job_id is not None
        return job_id


@pytest.mark.asyncio
async def test_concurrent_permission_revocation_waits_for_dispatch_validation_locks(
    isolated_v4_db_factory, monkeypatch
):
    db_factory = isolated_v4_db_factory
    run_context = await _summary_thread(db_factory, label="concurrent-revoke")
    base = run_context["base"]
    context = run_context["context"]
    thread = run_context["thread"]
    fence_validation_paused = asyncio.Event()
    resume_validation = asyncio.Event()
    dispatch_fenced = asyncio.Event()
    resume_after_fence = asyncio.Event()
    in_dispatch_fence = contextvars.ContextVar("in_summary_dispatch_fence", default=False)
    replay_original = SqlAlchemyThreadContextProvider.replay_history_sources
    mark_original = SummaryJobService.mark_dispatched

    async def pause_during_frozen_source_validation(
        provider, *, manifest, summary_source_policy=None
    ):
        if in_dispatch_fence.get() and not fence_validation_paused.is_set():
            fence_validation_paused.set()
            await resume_validation.wait()
        return await replay_original(
            provider,
            manifest=manifest,
            summary_source_policy=summary_source_policy,
        )

    async def pause_after_dispatch_commit(service, key, *, lease_token, now=None):
        token = in_dispatch_fence.set(True)
        try:
            await mark_original(service, key, lease_token=lease_token, now=now)
        finally:
            in_dispatch_fence.reset(token)
        job, dispatch = await _inspect_dispatch(db_factory, key=key)
        assert job.status == "DISPATCH_INTENT"
        assert dispatch.outcome == "DISPATCHED"
        dispatch_fenced.set()
        await resume_after_fence.wait()

    monkeypatch.setattr(
        SqlAlchemyThreadContextProvider,
        "replay_history_sources",
        pause_during_frozen_source_validation,
    )
    monkeypatch.setattr(SummaryJobService, "mark_dispatched", pause_after_dispatch_commit)

    async def probe_permission_row_lock() -> bool:
        async with db_factory() as session:
            try:
                async with session.begin():
                    await session.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    await session.scalar(
                        select(OrganizationMembership)
                        .where(
                            OrganizationMembership.organization_id == base["organization_id"],
                            OrganizationMembership.user_id == UUID(context.user_id),
                        )
                        .with_for_update()
                    )
                return False
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) == "55P03":
                    return True
                raise

    trigger = asyncio.create_task(
        run_context["thread_service"].submit_turn(
            context,
            thread.id,
            user_input="RECENT-TURN-04: concurrent access revocation must fence summary dispatch.",
            client_token=f"summary-fence-revoke-{uuid4().hex[:12]}",
        )
    )
    try:
        await asyncio.wait_for(fence_validation_paused.wait(), timeout=30)
        assert await probe_permission_row_lock() is True
        resume_validation.set()
        await asyncio.wait_for(dispatch_fenced.wait(), timeout=30)

        async with db_factory() as session:
            async with session.begin():
                membership = await session.scalar(
                    select(OrganizationMembership)
                    .where(
                        OrganizationMembership.organization_id == base["organization_id"],
                        OrganizationMembership.user_id == UUID(context.user_id),
                    )
                    .with_for_update()
                )
                assert membership is not None
                await session.delete(membership)
        resume_after_fence.set()
        submitted = await asyncio.wait_for(trigger, timeout=60)
    finally:
        resume_validation.set()
        resume_after_fence.set()
        if not trigger.done():
            await asyncio.wait_for(trigger, timeout=60)

    provider = run_context["provider"]
    assert not provider.summary_requests
    assert len(provider.main_requests) == 3
    job_id = await _job_id_for_run(db_factory, submitted.run_id)
    key = _summary_job_key(await _load_summary_job(db_factory, job_id))
    await _assert_terminal_unknown(
        db_factory,
        key=key,
        run_id=submitted.run_id,
        expected_failure="SUMMARY_DISPATCH_OUTCOME_UNKNOWN",
    )


async def _load_summary_job(db_factory, job_id: UUID):
    async with db_factory() as session:
        job = await session.get(ThreadSummaryJob, job_id)
        assert job is not None
        return job
