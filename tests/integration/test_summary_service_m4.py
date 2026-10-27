"""PostgreSQL fault-injection tests for the M4 durable summary service."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.agent_runtime.summary_models import (
    ThreadSummaryArtifact,
    ThreadSummaryDispatch,
    ThreadSummaryJob,
    ThreadSummaryJobSource,
    ThreadSummaryResponse,
)
from packages.agent_runtime.summary_prompt import (
    SummaryInputContext,
    SummaryPromptSource,
)
from packages.agent_runtime.summary_service import (
    NewSummaryJob,
    NewSummarySource,
    SummaryBindingVerifier,
    SummaryJobBinding,
    SummaryJobKey,
    SummaryJobService,
    SummaryResponseData,
    SummaryServiceError,
    SummaryTrustDecision,
    SummaryVerifierPhase,
)
from packages.agent_runtime.summary_state import SummaryJobStatus
from packages.control_plane.models import Workspace, WorkspaceMembership
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.evaluation.models import PricingSnapshot
from packages.evaluation.reproducibility import pricing_snapshot_content_hash

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
EXPECTED_DATABASE_URL = (
    "postgresql+psycopg://agenthub:m2_local_test@127.0.0.1:15432/agenthub_context_policy_m2"
)
EXPECTED_REVISION = "0042_summary_eligibility"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set the dedicated AGENTHUB_TEST_DATABASE_URL to run M4 service tests.",
    ),
]


def _async_database_url(database_url: str) -> URL:
    parsed = make_url(database_url)
    if parsed.drivername == "postgresql+psycopg":
        return parsed.set(drivername="postgresql+asyncpg")
    return parsed


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    assert TEST_DATABASE_URL == EXPECTED_DATABASE_URL, (
        "M4 service tests may only use the explicitly assigned isolated PostgreSQL database."
    )
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    del migrated_database
    database_engine, _ = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield database_engine
    finally:
        await database_engine.dispose()


@pytest_asyncio.fixture
async def factory(
    migrated_database: None,
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    del migrated_database
    return async_sessionmaker(engine, expire_on_commit=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Tenant:
    label: str
    user_id: UUID
    workspace_id: UUID
    thread_id: UUID
    run_id: UUID
    turn_id: UUID
    pricing_id: UUID
    source: NewSummarySource
    permission_snapshot: str


async def _seed_tenant(
    factory: async_sessionmaker[AsyncSession],
    *,
    label: str | None = None,
    permission_snapshot: str | None = None,
) -> _Tenant:
    from packages.agent_runtime.models import (
        Agent,
        AgentRun,
        AgentVersion,
    )
    from packages.control_plane.models import (
        Organization,
        OrganizationMembership,
        User,
        Workspace,
        WorkspaceMembership,
    )
    from packages.model_gateway.models import ModelProfile, ProviderCredential
    from packages.threads.models import AgentThread, ThreadTurn

    label = label or uuid4().hex
    permission_snapshot = permission_snapshot or f"permissions-{label}"
    email = f"summary-service-{label}@example.test"
    async with factory() as session:
        async with session.begin():
            user = User(
                id=uuid4(),
                email=email,
                normalized_email=email,
                password_hash="not-used",
            )
            session.add(user)
            await session.flush()
            organization = Organization(
                id=uuid4(),
                name=f"Summary Service {label}",
                created_by=user.id,
            )
            session.add(organization)
            await session.flush()
            session.add(
                OrganizationMembership(
                    organization_id=organization.id,
                    user_id=user.id,
                    role="OWNER",
                )
            )
            workspace = Workspace(
                id=uuid4(),
                organization_id=organization.id,
                name=f"Summary Service {label}",
            )
            session.add(workspace)
            await session.flush()
            session.add(
                WorkspaceMembership(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role="DEVELOPER",
                )
            )
            credential = ProviderCredential(
                id=uuid4(),
                workspace_id=workspace.id,
                provider="offline",
                name=f"summary-service-{label}",
                secret="not-used-by-test",
            )
            session.add(credential)
            await session.flush()
            profile = ModelProfile(
                id=uuid4(),
                workspace_id=workspace.id,
                provider_credential_id=credential.id,
                model="offline-summary-service",
                temperature=Decimal("0"),
                max_tokens=512,
                timeout_seconds=Decimal("5"),
                capabilities={"streaming": False},
            )
            session.add(profile)
            await session.flush()
            agent = Agent(
                id=uuid4(),
                workspace_id=workspace.id,
                name=f"summary-service-{label}",
                system_prompt="Offline summary persistence test.",
                prompt_version=1,
                model_profile_id=profile.id,
                knowledge_binding_mode="PINNED",
                model_retry_policy={"max_attempts": 1},
                retrieval_config={
                    "knowledge_binding_mode": "PINNED",
                    "knowledge_snapshots": [],
                },
                runtime_config={},
            )
            session.add(agent)
            await session.flush()
            version = AgentVersion(
                id=uuid4(),
                workspace_id=workspace.id,
                agent_id=agent.id,
                version_number=1,
                spec_schema_version=1,
                resolved_spec={
                    "spec_schema_version": 1,
                    "model": {"model": "offline-summary-service"},
                },
                resolved_spec_hash=_hash(f"version-{label}"),
                created_by=user.id,
            )
            session.add(version)
            await session.flush()
            thread = AgentThread(
                id=uuid4(),
                workspace_id=workspace.id,
                agent_id=agent.id,
                title=f"Summary Service {label}",
                kind="general",
                created_by=user.id,
            )
            session.add(thread)
            await session.flush()
            run = AgentRun(
                id=uuid4(),
                workspace_id=workspace.id,
                agent_version_id=version.id,
                thread_id=thread.id,
                input_text=f"source question {label}",
                final_output=f"source answer {label}",
                status="SUCCEEDED",
                created_by=user.id,
            )
            session.add(run)
            await session.flush()
            turn = ThreadTurn(
                id=uuid4(),
                workspace_id=workspace.id,
                thread_id=thread.id,
                sequence=1,
                user_input=f"source question {label}",
                agent_run_id=run.id,
                client_token=f"summary-service-{label}",
                input_hash=_hash(f"input-{label}"),
            )
            session.add(turn)
            await session.flush()
            run.thread_turn_id = turn.id
            pricing = PricingSnapshot(
                id=uuid4(),
                workspace_id=workspace.id,
                name=f"summary-service-price-{label}",
                provider="offline",
                model="offline-summary-service",
                currency="CNY",
                input_price_per_1m=Decimal("1.25000000"),
                output_price_per_1m=Decimal("2.50000000"),
                effective_at=datetime.now(UTC),
                source_note="isolated test price",
                content_hash="0" * 64,
                created_by=user.id,
            )
            pricing.content_hash = pricing_snapshot_content_hash(pricing)
            session.add(pricing)
            await session.flush()

            source = NewSummarySource(
                source_id=f"source-{label}",
                turn_id=turn.id,
                run_id=run.id,
                sequence=1,
                original_ref=f"thread_turns/{turn.id}",
                source_identity_hash=_hash(f"source-identity-{label}"),
                revision_sha256=_hash(f"source-revision-{label}"),
                user_input_sha256=_hash(f"source question {label}"),
                final_output_sha256=_hash(f"source answer {label}"),
                summary_eligibility="ELIGIBLE",
                eligibility_reason="completed_exchange",
            )
            return _Tenant(
                label=label,
                user_id=user.id,
                workspace_id=workspace.id,
                thread_id=thread.id,
                run_id=run.id,
                turn_id=turn.id,
                pricing_id=pricing.id,
                source=source,
                permission_snapshot=permission_snapshot,
            )


def _new_summary_job(tenant: _Tenant) -> NewSummaryJob:
    return NewSummaryJob(
        workspace_id=tenant.workspace_id,
        thread_id=tenant.thread_id,
        trigger_run_id=tenant.run_id,
        source_watermark=2,
        source_scope={
            "kind": "COMPLETED_PREFIX",
            "through_sequence": 1,
            "before_run_id": str(tenant.run_id),
        },
        policy_spec={"schema_version": 1, "mode": "single_dispatch"},
        policy_version="summary-policy-v1",
        prompt_version="summary_candidate_zh_v1",
        budget_version="summary-budget-v1",
        model_spec={"provider": "offline", "model": "offline-summary-service"},
        tokenizer_id="approx_language_v1",
        tokenizer_version="1",
        target_budget_tokens=1500,
        permission_snapshot_hash=_hash(tenant.permission_snapshot),
        created_by=tenant.user_id,
        sources=(tenant.source,),
    )


def _validation_context(tenant: _Tenant) -> SummaryInputContext:
    return SummaryInputContext(
        workspace_id=str(tenant.workspace_id),
        thread_id=str(tenant.thread_id),
        permission_snapshot=tenant.permission_snapshot,
        task_goal="Continue the task.",
        prefix_first_sequence=1,
        prefix_last_sequence=1,
        watermark=2,
        sources=(
            SummaryPromptSource(
                source_id=tenant.source.source_id,
                workspace_id=str(tenant.workspace_id),
                thread_id=str(tenant.thread_id),
                identity=tenant.source.source_identity_hash,
                revision=tenant.source.revision_sha256,
                sequence=1,
                watermark=2,
                role="user",
                speaker="Alice",
                occurred_at="2026-10-27T09:00:00+08:00",
                content=f"source question {tenant.label}",
                original_ref=tenant.source.original_ref,
                summary_eligible=True,
                exchange_complete=True,
            ),
        ),
        protected_facts=(),
        completed_actions=(),
        required_references=(),
        source_budget_tokens=2000,
        target_budget_tokens=1500,
        final_view_budget_tokens=3000,
        prefix_complete=True,
        protected_manifest_complete=True,
        action_manifest_complete=True,
        reference_manifest_complete=True,
    )


def _candidate(tenant: _Tenant) -> dict[str, object]:
    source_id = tenant.source.source_id
    return {
        "status": "OK",
        "goal": {"text": "Continue the task.", "source_ids": [source_id]},
        "decisions": [],
        "supported_facts": [
            {"text": "The source exchange is complete.", "source_ids": [source_id]}
        ],
        "conflicts": [],
        "open_questions": [],
        "completed_actions_refs": [],
        "next_steps": [],
        "omitted_source_ids": [],
    }


async def _seed_additional_exchange(
    factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> NewSummarySource:
    from packages.agent_runtime.models import AgentRun
    from packages.threads.models import ThreadTurn

    async with factory() as session:
        async with session.begin():
            agent_version_id = await session.scalar(
                select(AgentRun.agent_version_id)
                .join(
                    ThreadTurn,
                    (ThreadTurn.workspace_id == AgentRun.workspace_id)
                    & (ThreadTurn.agent_run_id == AgentRun.id),
                )
                .where(
                    ThreadTurn.workspace_id == tenant.workspace_id,
                    ThreadTurn.thread_id == tenant.thread_id,
                    ThreadTurn.id == tenant.turn_id,
                )
            )
            assert agent_version_id is not None
            run = AgentRun(
                id=uuid4(),
                workspace_id=tenant.workspace_id,
                agent_version_id=agent_version_id,
                thread_id=tenant.thread_id,
                input_text=f"source question {tenant.label} follow-up",
                final_output=f"source answer {tenant.label} follow-up",
                status="SUCCEEDED",
                created_by=tenant.user_id,
            )
            session.add(run)
            await session.flush()
            turn = ThreadTurn(
                id=uuid4(),
                workspace_id=tenant.workspace_id,
                thread_id=tenant.thread_id,
                sequence=2,
                user_input=f"source question {tenant.label} follow-up",
                agent_run_id=run.id,
                client_token=f"summary-service-{tenant.label}-2",
                input_hash=_hash(f"input-{tenant.label}-2"),
            )
            session.add(turn)
            await session.flush()
            run.thread_turn_id = turn.id
            return NewSummarySource(
                source_id=f"source-{tenant.label}-2",
                turn_id=turn.id,
                run_id=run.id,
                sequence=2,
                original_ref=f"thread_turns/{turn.id}",
                source_identity_hash=_hash(f"source-identity-{tenant.label}-2"),
                revision_sha256=_hash(f"source-revision-{tenant.label}-2"),
                user_input_sha256=_hash(f"source question {tenant.label} follow-up"),
                final_output_sha256=_hash(f"source answer {tenant.label} follow-up"),
                summary_eligibility="ELIGIBLE",
                eligibility_reason="completed_exchange",
            )


def _two_source_context(
    tenant: _Tenant,
    second_source: NewSummarySource,
) -> SummaryInputContext:
    return SummaryInputContext(
        workspace_id=str(tenant.workspace_id),
        thread_id=str(tenant.thread_id),
        permission_snapshot=tenant.permission_snapshot,
        task_goal="Continue the task.",
        prefix_first_sequence=1,
        prefix_last_sequence=2,
        watermark=3,
        sources=(
            SummaryPromptSource(
                source_id=tenant.source.source_id,
                workspace_id=str(tenant.workspace_id),
                thread_id=str(tenant.thread_id),
                identity=tenant.source.source_identity_hash,
                revision=tenant.source.revision_sha256,
                sequence=1,
                watermark=3,
                role="user",
                speaker="Alice",
                occurred_at="2026-10-27T09:00:00+08:00",
                content=f"source question {tenant.label}",
                original_ref=tenant.source.original_ref,
                summary_eligible=True,
                exchange_complete=True,
            ),
            SummaryPromptSource(
                source_id=second_source.source_id,
                workspace_id=str(tenant.workspace_id),
                thread_id=str(tenant.thread_id),
                identity=second_source.source_identity_hash,
                revision=second_source.revision_sha256,
                sequence=2,
                watermark=3,
                role="user",
                speaker="Alice",
                occurred_at="2026-10-27T09:05:00+08:00",
                content=f"source question {tenant.label} follow-up",
                original_ref=second_source.original_ref,
                summary_eligible=True,
                exchange_complete=True,
            ),
        ),
        protected_facts=(),
        completed_actions=(),
        required_references=(),
        source_budget_tokens=2000,
        target_budget_tokens=1500,
        final_view_budget_tokens=3000,
        prefix_complete=True,
        protected_manifest_complete=True,
        action_manifest_complete=True,
        reference_manifest_complete=True,
    )


def _two_source_candidate(
    tenant: _Tenant,
    second_source: NewSummarySource,
) -> dict[str, object]:
    source_ids = [tenant.source.source_id, second_source.source_id]
    return {
        "status": "OK",
        "goal": {"text": "Continue the task.", "source_ids": source_ids},
        "decisions": [],
        "supported_facts": [
            {"text": "Both source exchanges are complete.", "source_ids": source_ids}
        ],
        "conflicts": [],
        "open_questions": [],
        "completed_actions_refs": [],
        "next_steps": [],
        "omitted_source_ids": [],
    }


class _Verifier:
    def __init__(
        self,
        context_factory: Callable[[SummaryJobBinding], SummaryInputContext],
    ) -> None:
        self.context_factory = context_factory
        self.phases: list[SummaryVerifierPhase] = []

    async def verify(
        self,
        session: AsyncSession,
        binding: SummaryJobBinding,
    ) -> SummaryTrustDecision:
        del session
        self.phases.append(binding.phase)
        if binding.phase is SummaryVerifierPhase.PUBLICATION:
            return SummaryTrustDecision.verified()
        return SummaryTrustDecision.verified(self.context_factory(binding))


class _DbBackedVerifier:
    """Test-only trusted verifier that reloads membership from PostgreSQL."""

    def __init__(self, tenant: _Tenant) -> None:
        self.tenant = tenant
        self.phases: list[SummaryVerifierPhase] = []

    async def verify(
        self,
        session: AsyncSession,
        binding: SummaryJobBinding,
    ) -> SummaryTrustDecision:
        self.phases.append(binding.phase)
        if binding.phase is SummaryVerifierPhase.PUBLICATION:
            return SummaryTrustDecision.verified()
        membership = await session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == binding.workspace_id,
                WorkspaceMembership.user_id == binding.created_by,
            )
        )
        if membership is None:
            return SummaryTrustDecision.denied("PERMISSION_REVOKED")
        return SummaryTrustDecision.verified(_validation_context(self.tenant))


class _TestBudgetReserver:
    """Test-only seam; this is not a shared production budget."""

    async def reserve(self, session, *, binding, reservation_id, amount, currency):
        del session, binding, reservation_id
        return amount >= 0 and currency == "CNY"


async def _create_claimed(
    factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
    *,
    verifier: SummaryBindingVerifier | None = None,
) -> tuple[SummaryJobService, SummaryJobKey, str]:
    if verifier is None:
        verifier = _Verifier(lambda binding: _validation_context(tenant))
    service = SummaryJobService(factory, verifier=verifier, budget_reserver=_TestBudgetReserver())
    created = await service.create_job(_new_summary_job(tenant))
    claimed = await service.claim(created.key)
    assert claimed is not None
    return service, created.key, claimed.lease_token


async def _commit_intent(
    service: SummaryJobService,
    key: SummaryJobKey,
    lease_token: str,
    *,
    tenant: _Tenant,
) -> None:
    await service.commit_dispatch_intent(
        key,
        lease_token=lease_token,
        pricing_snapshot_id=tenant.pricing_id,
        reserved_input_tokens=700,
        reserved_output_tokens=300,
    )
    await service.mark_dispatched(key, lease_token=lease_token)


async def _store_response(
    service: SummaryJobService,
    key: SummaryJobKey,
    lease_token: str,
    tenant: _Tenant,
) -> UUID:
    stored = await service.store_provider_response(
        key,
        lease_token=lease_token,
        response=SummaryResponseData(
            provider="offline",
            model="offline-summary-service",
            raw_response=_candidate(tenant),
            usage_metadata={"source": "offline"},
            input_tokens=700,
            output_tokens=300,
            total_tokens=1000,
            finish_reason="stop",
        ),
    )
    return stored.response_id


async def _validated_with_pending_artifact(
    factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
    *,
    verifier: SummaryBindingVerifier | None = None,
) -> tuple[SummaryJobService, SummaryJobKey, str, UUID]:
    verifier = verifier or _Verifier(lambda binding: _validation_context(tenant))
    service, key, first_lease = await _create_claimed(
        factory,
        tenant,
        verifier=verifier,
    )
    await _commit_intent(service, key, first_lease, tenant=tenant)
    response_id = await _store_response(service, key, first_lease, tenant)

    restarted = SummaryJobService(factory, verifier=verifier)
    recovery = await restarted.claim_recovery(key)
    assert recovery is not None
    settled = await restarted.validate_stored_response(
        key,
        lease_token=recovery.lease_token,
    )
    assert settled.status == SummaryJobStatus.VALIDATED.value
    assert settled.artifact_id is not None
    return restarted, key, recovery.lease_token, response_id


async def _validated_two_source_with_pending_artifact(
    factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
) -> tuple[SummaryJobService, SummaryJobKey, str, NewSummarySource, UUID]:
    second_source = await _seed_additional_exchange(factory, tenant)
    verifier = _Verifier(lambda binding: _two_source_context(tenant, second_source))
    service = SummaryJobService(factory, verifier=verifier, budget_reserver=_TestBudgetReserver())
    created = await service.create_job(
        NewSummaryJob(
            workspace_id=tenant.workspace_id,
            thread_id=tenant.thread_id,
            trigger_run_id=second_source.run_id,
            source_watermark=3,
            source_scope={
                "kind": "COMPLETED_PREFIX",
                "through_sequence": 2,
                "before_run_id": str(second_source.run_id),
            },
            policy_spec={"schema_version": 1, "mode": "single_dispatch"},
            policy_version="summary-policy-v1",
            prompt_version="summary_candidate_zh_v1",
            budget_version="summary-budget-v1",
            model_spec={"provider": "offline", "model": "offline-summary-service"},
            tokenizer_id="approx_language_v1",
            tokenizer_version="1",
            target_budget_tokens=1500,
            permission_snapshot_hash=_hash(tenant.permission_snapshot),
            created_by=tenant.user_id,
            sources=(tenant.source, second_source),
        )
    )
    claimed = await service.claim(created.key)
    assert claimed is not None
    await _commit_intent(service, created.key, claimed.lease_token, tenant=tenant)
    stored = await service.store_provider_response(
        created.key,
        lease_token=claimed.lease_token,
        response=SummaryResponseData(
            provider="offline",
            model="offline-summary-service",
            raw_response=_two_source_candidate(tenant, second_source),
            usage_metadata={"source": "offline"},
            input_tokens=700,
            output_tokens=300,
            total_tokens=1000,
            finish_reason="stop",
        ),
    )
    restarted = SummaryJobService(factory, verifier=verifier)
    recovery = await restarted.claim_recovery(created.key)
    assert recovery is not None
    settled = await restarted.validate_stored_response(
        created.key,
        lease_token=recovery.lease_token,
    )
    assert settled.status == SummaryJobStatus.VALIDATED.value
    assert settled.artifact_id is not None
    return (
        restarted,
        created.key,
        recovery.lease_token,
        second_source,
        stored.response_id,
    )


async def _job_row(
    factory: async_sessionmaker[AsyncSession],
    key: SummaryJobKey,
) -> tuple[ThreadSummaryJob, ThreadSummaryDispatch, list[ThreadSummaryResponse]]:
    async with factory() as session:
        job = await session.get(ThreadSummaryJob, key.job_id)
        assert job is not None
        dispatch = await session.scalar(
            select(ThreadSummaryDispatch).where(
                ThreadSummaryDispatch.workspace_id == key.workspace_id,
                ThreadSummaryDispatch.thread_id == key.thread_id,
                ThreadSummaryDispatch.job_id == key.job_id,
            )
        )
        assert dispatch is not None
        responses = list(
            await session.scalars(
                select(ThreadSummaryResponse)
                .where(
                    ThreadSummaryResponse.workspace_id == key.workspace_id,
                    ThreadSummaryResponse.thread_id == key.thread_id,
                    ThreadSummaryResponse.job_id == key.job_id,
                )
                .order_by(ThreadSummaryResponse.stored_at)
            )
        )
        return job, dispatch, responses


async def _artifact_rows(
    factory: async_sessionmaker[AsyncSession],
    key: SummaryJobKey,
) -> list[ThreadSummaryArtifact]:
    async with factory() as session:
        return list(
            await session.scalars(
                select(ThreadSummaryArtifact)
                .where(
                    ThreadSummaryArtifact.workspace_id == key.workspace_id,
                    ThreadSummaryArtifact.thread_id == key.thread_id,
                    ThreadSummaryArtifact.job_id == key.job_id,
                )
                .order_by(ThreadSummaryArtifact.revision_number)
            )
        )


@pytest.mark.asyncio
async def test_concurrent_create_deduplicates_one_immutable_job_and_source_manifest(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service = SummaryJobService(factory)
    command = _new_summary_job(tenant)

    results = await asyncio.gather(
        service.create_job(command),
        service.create_job(command),
        service.create_job(command),
    )
    assert sum(result.created for result in results) == 1
    assert len({result.key for result in results}) == 1
    assert len({result.identity_hash for result in results}) == 1
    async with factory() as session:
        jobs = list(
            await session.scalars(
                select(ThreadSummaryJob).where(
                    ThreadSummaryJob.workspace_id == tenant.workspace_id,
                    ThreadSummaryJob.thread_id == tenant.thread_id,
                )
            )
        )
        source_count = await session.scalar(
            select(func.count())
            .select_from(ThreadSummaryJobSource)
            .where(
                ThreadSummaryJobSource.workspace_id == tenant.workspace_id,
                ThreadSummaryJobSource.thread_id == tenant.thread_id,
                ThreadSummaryJobSource.job_id == results[0].key.job_id,
            )
        )
    assert len(jobs) == 1
    assert source_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_after_write", [False, True])
async def test_unavailable_shared_budget_never_dispatches_and_rolls_back(
    factory: async_sessionmaker[AsyncSession], reject_after_write: bool
) -> None:
    tenant = await _seed_tenant(factory)

    class RejectedBudget:
        async def reserve(self, session, *, binding, reservation_id, amount, currency):
            del binding, reservation_id, amount, currency
            await session.execute(
                update(Workspace).where(Workspace.id == tenant.workspace_id).values(name="changed")
            )
            return False

    options = {"budget_reserver": RejectedBudget()} if reject_after_write else {}
    service = SummaryJobService(
        factory, verifier=_Verifier(lambda binding: _validation_context(tenant)), **options
    )
    created = await service.create_job(_new_summary_job(tenant))
    claimed = await service.claim(created.key)
    assert claimed is not None
    async with factory() as session:
        original_name = await session.scalar(
            select(Workspace.name).where(Workspace.id == tenant.workspace_id)
        )
    with pytest.raises(SummaryServiceError) as blocked:
        await _commit_intent(service, created.key, claimed.lease_token, tenant=tenant)
    assert blocked.value.code == "SUMMARY_SHARED_BUDGET_UNAVAILABLE"
    async with factory() as session:
        name = await session.scalar(
            select(Workspace.name).where(Workspace.id == tenant.workspace_id)
        )
        job = await session.get(ThreadSummaryJob, created.key.job_id)
        count = await session.scalar(
            select(func.count()).select_from(ThreadSummaryDispatch).where(
                ThreadSummaryDispatch.job_id == created.key.job_id
            )
        )
    assert name == original_name
    assert job.status == "CLAIMED" and job.dispatch_id is None and not job.budget_reserved
    assert count == 0


@pytest.mark.asyncio
async def test_thread_deletion_reports_retained_summary_job(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from packages.core.errors.exceptions import AgentHubError
    from packages.core.execution_context.models import (
        OrganizationContext,
        PrincipalContext,
        WorkspaceExecutionContext,
    )
    from packages.threads.service import ThreadService

    tenant = await _seed_tenant(factory)
    await SummaryJobService(factory).create_job(_new_summary_job(tenant))
    context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="test", trace_id="test", user_id=str(tenant.user_id)
            ),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(tenant.workspace_id),
        permissions=frozenset({"agent_run"}),
    )
    with pytest.raises(AgentHubError) as blocked:
        await ThreadService(factory).delete_thread(context, tenant.thread_id)
    assert blocked.value.code == "SUMMARY_EVIDENCE_RETAINED"
    assert blocked.value.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_claim_has_one_owner_and_thread_tenant_lookup_fences_everyone_else(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    other = await _seed_tenant(factory)
    service = SummaryJobService(factory)
    created = await service.create_job(_new_summary_job(tenant))

    claims = await asyncio.gather(
        service.claim(created.key),
        service.claim(created.key),
    )
    assert sum(claim is not None for claim in claims) == 1
    winner = next(claim for claim in claims if claim is not None)

    wrong_thread_key = SummaryJobKey(
        created.key.workspace_id,
        other.thread_id,
        created.key.job_id,
    )
    with pytest.raises(SummaryServiceError) as wrong_thread:
        await service.commit_dispatch_intent(
            wrong_thread_key,
            lease_token=winner.lease_token,
            pricing_snapshot_id=tenant.pricing_id,
            reserved_input_tokens=10,
            reserved_output_tokens=10,
        )
    assert wrong_thread.value.code == "SUMMARY_JOB_NOT_FOUND"

    wrong_tenant_key = SummaryJobKey(
        other.workspace_id,
        created.key.thread_id,
        created.key.job_id,
    )
    with pytest.raises(SummaryServiceError) as wrong_tenant:
        await service.commit_dispatch_intent(
            wrong_tenant_key,
            lease_token=winner.lease_token,
            pricing_snapshot_id=tenant.pricing_id,
            reserved_input_tokens=10,
            reserved_output_tokens=10,
        )
    assert wrong_tenant.value.code == "SUMMARY_JOB_NOT_FOUND"

    async with factory() as session:
        dispatch_count = await session.scalar(
            select(func.count())
            .select_from(ThreadSummaryDispatch)
            .where(
                ThreadSummaryDispatch.workspace_id == created.key.workspace_id,
                ThreadSummaryDispatch.thread_id == created.key.thread_id,
                ThreadSummaryDispatch.job_id == created.key.job_id,
            )
        )
    assert dispatch_count == 0


@pytest.mark.asyncio
async def test_prepared_dispatch_contains_verified_prompt_and_no_provider_effect(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    prepared = await service.prepare_dispatch(
        key, lease_token=lease_token, reserved_input_tokens=700, reserved_output_tokens=300
    )
    assert prepared.model_spec == {"provider": "offline", "model": "offline-summary-service"}
    assert "source question" in prepared.prompt.user_prompt
    async with factory() as session:
        job = await session.get(ThreadSummaryJob, key.job_id)
        dispatch_count = await session.scalar(
            select(func.count())
            .select_from(ThreadSummaryDispatch)
            .where(ThreadSummaryDispatch.job_id == key.job_id)
        )
    assert job is not None and job.status == "CLAIMED"
    assert dispatch_count == 0
    await service.commit_dispatch_intent(
        key,
        lease_token=lease_token,
        pricing_snapshot_id=tenant.pricing_id,
        reserved_input_tokens=700,
        reserved_output_tokens=300,
        request_spec=prepared.request_spec,
    )
    await service.mark_dispatched(key, lease_token=lease_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_summary_worker_and_durable_service_share_one_fake_gateway_dispatch(
    factory: async_sessionmaker[AsyncSession], provider_fails: bool
) -> None:
    from packages.agent_runtime.summary_worker import SummaryWorker, frozen_summary_model_spec
    from packages.core.execution_context.models import (
        OrganizationContext,
        PrincipalContext,
        WorkspaceExecutionContext,
    )
    from packages.model_gateway.contracts import ModelResponse, ModelUsage
    from tests.unit.test_context_policy_m2_runtime import _freeze, _profile
    from tests.unit.test_summary_worker_m4 import _summary_policy

    tenant = await _seed_tenant(factory)
    profile = replace(
        _profile(total=32768, output=2048, workspace=tenant.workspace_id),
        model="offline-summary-service",
    )
    spec = _freeze((profile,))
    spec = replace(spec, runtime={**spec.runtime, "summary_policy": _summary_policy()})
    verifier = _Verifier(lambda binding: _validation_context(tenant))
    service = SummaryJobService(factory, verifier=verifier, budget_reserver=_TestBudgetReserver())
    created = await service.create_job(
        replace(
            _new_summary_job(tenant),
            policy_version="thread_summary_policy_v1",
            model_spec=frozen_summary_model_spec(spec.model_plan.primary),
        )
    )
    claimed = await service.claim(created.key)
    assert claimed is not None
    context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="test", trace_id="test", user_id=str(tenant.user_id)
            ),
            organization_id=str(uuid4()),
            org_role="owner",
        ),
        workspace_id=str(tenant.workspace_id),
    )

    class FakeGateway:
        calls = 0

        async def generate_resolved(self, current_context, model_plan, request):
            self.calls += 1
            assert current_context == context
            assert not model_plan.fallbacks and request.retry_policy.max_attempts == 1
            if provider_fails:
                raise TimeoutError("provider outcome unknown")
            return ModelResponse(
                content=json.dumps(_candidate(tenant)),
                provider="offline",
                model="offline-summary-service",
                finish_reason="stop",
                usage=ModelUsage(input_tokens=700, output_tokens=300, total_tokens=1000),
            )

    gateway = FakeGateway()
    result = await SummaryWorker(service, gateway).run_claimed(
        claimed=claimed,
        context=context,
        frozen_spec=spec,
        pricing_snapshot_id=tenant.pricing_id,
    )
    job, dispatch, responses = await _job_row(factory, created.key)
    assert gateway.calls == 1
    if provider_fails:
        assert result.status == job.status == dispatch.outcome == "UNKNOWN_OUTCOME"
        assert not responses and job.budget_reserved
    else:
        assert result.status == job.status == "VALIDATED"
        assert dispatch.outcome == "RESPONSE_RECEIVED" and len(responses) == 1
        assert (await _artifact_rows(factory, created.key))[0].publication_status == "PENDING"


@pytest.mark.asyncio
async def test_dispatch_rejects_request_spec_not_built_from_verified_context(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    with pytest.raises(SummaryServiceError) as error:
        await service.commit_dispatch_intent(
            key,
            lease_token=lease_token,
            pricing_snapshot_id=tenant.pricing_id,
            reserved_input_tokens=700,
            reserved_output_tokens=300,
            request_spec={"messages": "different unverified content"},
        )
    assert error.value.code == "SUMMARY_DISPATCH_PAYLOAD_MISMATCH"
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ThreadSummaryDispatch)
                .where(ThreadSummaryDispatch.job_id == key.job_id)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_dispatch_precommit_rechecks_actor_permission_and_writes_no_intent_or_reservation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    verifier = _DbBackedVerifier(tenant)
    service, key, lease_token = await _create_claimed(
        factory,
        tenant,
        verifier=verifier,
    )
    async with factory() as session:
        async with session.begin():
            membership = await session.get(
                WorkspaceMembership,
                (tenant.workspace_id, tenant.user_id),
                with_for_update=True,
            )
            assert membership is not None
            await session.delete(membership)

    with pytest.raises(SummaryServiceError) as revoked:
        await service.commit_dispatch_intent(
            key,
            lease_token=lease_token,
            pricing_snapshot_id=tenant.pricing_id,
            reserved_input_tokens=700,
            reserved_output_tokens=300,
        )
    assert revoked.value.code == "PERMISSION_REVOKED"
    assert verifier.phases == [SummaryVerifierPhase.DISPATCH]

    async with factory() as session:
        job = await session.get(ThreadSummaryJob, key.job_id)
    assert job is not None
    assert job.status == "CLAIMED"
    assert job.budget_reserved is False
    assert job.dispatch_id is None
    async with factory() as session:
        dispatch_count = await session.scalar(
            select(func.count())
            .select_from(ThreadSummaryDispatch)
            .where(
                ThreadSummaryDispatch.workspace_id == key.workspace_id,
                ThreadSummaryDispatch.thread_id == key.thread_id,
                ThreadSummaryDispatch.job_id == key.job_id,
            )
        )
    assert dispatch_count == 0


@pytest.mark.asyncio
async def test_claim_then_worker_crash_requeues_and_expiry_never_infers_dispatch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, crash_lease = await _create_claimed(factory, tenant)
    now = datetime.now(UTC)
    async with factory() as session:
        async with session.begin():
            job = await session.get(ThreadSummaryJob, key.job_id, with_for_update=True)
            assert job is not None
            job.lease_expires_at = now - timedelta(seconds=1)

    recovered = await SummaryJobService(factory).recover_expired_leases(
        workspace_id=key.workspace_id,
        thread_id=key.thread_id,
        now=now,
    )
    assert [(row.previous_status, row.current_status) for row in recovered] == [
        ("CLAIMED", "QUEUED")
    ]
    replacement = await service.claim(key)
    assert replacement is not None
    assert replacement.lease_token != crash_lease
    with pytest.raises(SummaryServiceError) as stale_owner:
        await service.commit_dispatch_intent(
            key,
            lease_token=crash_lease,
            pricing_snapshot_id=tenant.pricing_id,
            reserved_input_tokens=700,
            reserved_output_tokens=300,
        )
    assert stale_owner.value.code == "SUMMARY_LEASE_LOST"


@pytest.mark.asyncio
async def test_dispatch_intent_budget_ledger_and_pointer_commit_together_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    intent = await service.commit_dispatch_intent(
        key,
        lease_token=lease_token,
        pricing_snapshot_id=tenant.pricing_id,
        reserved_input_tokens=700,
        reserved_output_tokens=300,
    )
    await service.mark_dispatched(key, lease_token=lease_token)
    job, dispatch, responses = await _job_row(factory, key)
    assert job.status == "DISPATCH_INTENT"
    assert job.budget_reserved is True
    assert job.dispatch_id == intent.dispatch_id == dispatch.id
    assert dispatch.reservation_id == intent.reservation_id
    assert dispatch.outcome == "DISPATCHED"
    assert dispatch.reserved_input_tokens == 700
    assert dispatch.reserved_output_tokens == 300
    assert not responses

    with pytest.raises(SummaryServiceError) as duplicate:
        await service.commit_dispatch_intent(
            key,
            lease_token=lease_token,
            pricing_snapshot_id=tenant.pricing_id,
            reserved_input_tokens=700,
            reserved_output_tokens=300,
        )
    assert duplicate.value.code == "SUMMARY_DISPATCH_STATE_INVALID"


@pytest.mark.asyncio
async def test_dispatch_boundary_cannot_be_crossed_twice_even_with_same_live_lease(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    await service.commit_dispatch_intent(
        key,
        lease_token=lease_token,
        pricing_snapshot_id=tenant.pricing_id,
        reserved_input_tokens=700,
        reserved_output_tokens=300,
    )

    results = await asyncio.gather(
        service.mark_dispatched(key, lease_token=lease_token),
        service.mark_dispatched(key, lease_token=lease_token),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    failures = [result for result in results if isinstance(result, SummaryServiceError)]
    assert len(failures) == 1
    assert failures[0].code == "SUMMARY_DISPATCH_ALREADY_STARTED"
    _, dispatch, _ = await _job_row(factory, key)
    assert dispatch.outcome == "DISPATCHED"


@pytest.mark.asyncio
async def test_unconfirmed_provider_outcome_is_fenced_before_lease_expiry(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    await _commit_intent(service, key, lease_token, tenant=tenant)
    with pytest.raises(SummaryServiceError) as wrong_owner:
        await service.mark_dispatch_unknown(key, lease_token="wrong-owner")
    assert wrong_owner.value.code == "SUMMARY_LEASE_LOST"
    outcome = await service.mark_dispatch_unknown(key, lease_token=lease_token)
    assert outcome.status == "UNKNOWN_OUTCOME"
    job, dispatch, responses = await _job_row(factory, key)
    assert job.budget_reserved is True
    assert dispatch.outcome == "UNKNOWN_OUTCOME"
    assert dispatch.reserved_cost_amount > 0
    assert responses == []
    assert await service.claim(key) is None


@pytest.mark.asyncio
async def test_permission_revoked_after_intent_blocks_the_provider_boundary(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(
        factory, tenant, verifier=_DbBackedVerifier(tenant)
    )
    await service.commit_dispatch_intent(
        key,
        lease_token=lease_token,
        pricing_snapshot_id=tenant.pricing_id,
        reserved_input_tokens=700,
        reserved_output_tokens=300,
    )
    async with factory() as session:
        async with session.begin():
            membership = await session.get(
                WorkspaceMembership, (tenant.workspace_id, tenant.user_id), with_for_update=True
            )
            assert membership is not None
            await session.delete(membership)
    with pytest.raises(SummaryServiceError) as revoked:
        await service.mark_dispatched(key, lease_token=lease_token)
    assert revoked.value.code == "PERMISSION_REVOKED"
    _, dispatch, responses = await _job_row(factory, key)
    assert dispatch.outcome == "INTENT_RECORDED"
    assert not responses


@pytest.mark.asyncio
async def test_changed_verified_prompt_after_intent_blocks_the_provider_boundary(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    goal = {"value": "Original frozen task."}
    verifier = _Verifier(
        lambda binding: replace(_validation_context(tenant), task_goal=goal["value"])
    )
    service, key, lease_token = await _create_claimed(factory, tenant, verifier=verifier)
    await service.commit_dispatch_intent(
        key,
        lease_token=lease_token,
        pricing_snapshot_id=tenant.pricing_id,
        reserved_input_tokens=700,
        reserved_output_tokens=300,
    )
    goal["value"] = "Changed task after the intent was recorded."
    with pytest.raises(SummaryServiceError) as changed:
        await service.mark_dispatched(key, lease_token=lease_token)
    assert changed.value.code == "SUMMARY_DISPATCH_PAYLOAD_MISMATCH"
    _, dispatch, responses = await _job_row(factory, key)
    assert dispatch.outcome == "INTENT_RECORDED"
    assert not responses


@pytest.mark.asyncio
async def test_wrong_live_owner_token_cannot_store_response_or_force_unknown(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    await _commit_intent(service, key, lease_token, tenant=tenant)

    with pytest.raises(SummaryServiceError) as wrong_owner:
        await service.store_provider_response(
            key,
            lease_token="not-the-current-owner",
            response=SummaryResponseData(
                provider="offline",
                model="offline-summary-service",
                raw_response=_candidate(tenant),
                usage_metadata={"source": "wrong-owner"},
            ),
        )
    assert wrong_owner.value.code == "SUMMARY_LEASE_LOST"

    job, dispatch, responses = await _job_row(factory, key)
    assert job.status == "DISPATCH_INTENT"
    assert job.lease_token == lease_token
    assert dispatch.outcome == "DISPATCHED"
    assert not responses

    response_id = await _store_response(service, key, lease_token, tenant)
    job, dispatch, responses = await _job_row(factory, key)
    assert job.status == "RESPONSE_STORED"
    assert job.response_id == response_id
    assert dispatch.outcome == "RESPONSE_RECEIVED"
    assert len(responses) == 1


@pytest.mark.asyncio
async def test_expired_post_dispatch_lease_becomes_irreversible_unknown_with_cost_retained(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    await _commit_intent(service, key, lease_token, tenant=tenant)
    now = datetime.now(UTC)
    async with factory() as session:
        async with session.begin():
            job = await session.get(ThreadSummaryJob, key.job_id, with_for_update=True)
            assert job is not None
            job.lease_expires_at = now - timedelta(seconds=1)

    recovered = await SummaryJobService(factory).recover_expired_leases(
        workspace_id=key.workspace_id,
        thread_id=key.thread_id,
        now=now,
    )
    assert [(row.previous_status, row.current_status) for row in recovered] == [
        ("DISPATCH_INTENT", "UNKNOWN_OUTCOME")
    ]
    job, dispatch, responses = await _job_row(factory, key)
    assert job.status == "UNKNOWN_OUTCOME"
    assert job.lease_token is None
    assert job.budget_reserved is True
    assert job.dispatch_id == dispatch.id
    assert dispatch.outcome == "UNKNOWN_OUTCOME"
    assert dispatch.reserved_cost_amount > 0
    assert not responses

    dispatch_id = dispatch.id
    reserved_cost = dispatch.reserved_cost_amount
    second_claim = await service.claim(key)
    assert second_claim is None
    with pytest.raises(SummaryServiceError) as terminal:
        await service.renew_lease(key, lease_token=lease_token, now=now)
    assert terminal.value.code == "SUMMARY_LEASE_NOT_RENEWABLE"

    with pytest.raises(SummaryServiceError) as late_response:
        await service.store_provider_response(
            key,
            lease_token=lease_token,
            response=SummaryResponseData(
                provider="offline",
                model="offline-summary-service",
                raw_response=_candidate(tenant),
                usage_metadata={"source": "late"},
            ),
            now=now,
        )
    assert late_response.value.code == "SUMMARY_UNKNOWN_OUTCOME_TERMINAL"
    job_after, dispatch_after, responses_after = await _job_row(factory, key)
    assert job_after.status == "UNKNOWN_OUTCOME"
    assert job_after.dispatch_id == dispatch_id
    assert job_after.budget_reserved is True
    assert dispatch_after.outcome == "UNKNOWN_OUTCOME"
    assert dispatch_after.reserved_cost_amount == reserved_cost
    assert not responses_after


@pytest.mark.asyncio
async def test_response_is_persisted_before_validation_and_offline_restart_recovers_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    verifier = _Verifier(lambda binding: _validation_context(tenant))
    service, key, lease_token = await _create_claimed(
        factory,
        tenant,
        verifier=verifier,
    )
    await _commit_intent(service, key, lease_token, tenant=tenant)
    response_id = await _store_response(service, key, lease_token, tenant)

    persisted_job, dispatch, responses = await _job_row(factory, key)
    assert persisted_job.status == "RESPONSE_STORED"
    assert persisted_job.response_id == response_id
    assert persisted_job.artifact_id is None
    assert dispatch.outcome == "RESPONSE_RECEIVED"
    assert len(responses) == 1
    assert responses[0].raw_response["format"] == "summary_raw_base64_v1"
    assert json.loads(base64.b64decode(responses[0].raw_response["data"])) == _candidate(tenant)
    assert verifier.phases == [SummaryVerifierPhase.DISPATCH, SummaryVerifierPhase.DISPATCH]

    with pytest.raises(SummaryServiceError) as no_lease:
        await SummaryJobService(factory, verifier=verifier).validate_stored_response(
            key,
            lease_token=lease_token,
        )
    assert no_lease.value.code == "SUMMARY_LEASE_LOST"
    job_after_rejection, _, responses_after_rejection = await _job_row(factory, key)
    assert job_after_rejection.status == "RESPONSE_STORED"
    assert job_after_rejection.artifact_id is None
    assert len(responses_after_rejection) == 1
    assert verifier.phases == [SummaryVerifierPhase.DISPATCH, SummaryVerifierPhase.DISPATCH]

    restarted = SummaryJobService(factory, verifier=verifier)
    recovery = await restarted.claim_recovery(key)
    assert recovery is not None
    with pytest.raises(SummaryServiceError) as stale_lease:
        await restarted.validate_stored_response(
            key,
            lease_token=lease_token,
        )
    assert stale_lease.value.code == "SUMMARY_LEASE_LOST"
    settled = await restarted.validate_stored_response(
        key,
        lease_token=recovery.lease_token,
    )
    assert settled.status == "VALIDATED"
    assert settled.response_id == response_id
    assert settled.artifact_id is not None
    assert verifier.phases == [
        SummaryVerifierPhase.DISPATCH,
        SummaryVerifierPhase.DISPATCH,
        SummaryVerifierPhase.RESPONSE_VALIDATION,
    ]

    after_restart = await restarted.get_settlement(key)
    assert after_restart.status == "VALIDATED"
    assert after_restart.response_id == response_id
    assert after_restart.artifact_id == settled.artifact_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "cost", "currency", "expected"),
    [
        (None, None, None, None, "SUMMARY_PROVIDER_USAGE_UNVERIFIED"),
        (701, 300, None, None, "SUMMARY_PROVIDER_USAGE_EXCEEDS_RESERVATION"),
        (700, 301, None, None, "SUMMARY_PROVIDER_USAGE_EXCEEDS_RESERVATION"),
        (700, 300, Decimal("1"), "CNY", "SUMMARY_PROVIDER_COST_EXCEEDS_RESERVATION"),
        (700, 300, Decimal("0.0001"), "USD", "SUMMARY_PROVIDER_COST_CURRENCY_MISMATCH"),
    ],
)
async def test_response_outside_reservation_is_stored_but_cannot_publish(
    factory: async_sessionmaker[AsyncSession], input_tokens, output_tokens, cost, currency, expected
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    await _commit_intent(service, key, lease_token, tenant=tenant)
    stored = await service.store_provider_response(
        key,
        lease_token=lease_token,
        response=SummaryResponseData(
            provider="offline",
            model="offline-summary-service",
            raw_response=_candidate(tenant),
            usage_metadata={"source": "offline"},
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=(
                None if input_tokens is None or output_tokens is None
                else input_tokens + output_tokens
            ),
            cost_amount=cost,
            cost_currency=currency,
            cost_is_estimate=True if cost is not None else None,
            finish_reason="stop",
        ),
    )
    recovery = await service.claim_recovery(key)
    assert recovery is not None
    result = await service.validate_stored_response(key, lease_token=recovery.lease_token)
    assert result.status == SummaryJobStatus.FAILED.value
    assert result.artifact_id is None
    assert result.validation_result["issues"][0]["code"] == expected
    job, dispatch, responses = await _job_row(factory, key)
    assert job.response_id == stored.response_id
    assert dispatch.outcome == "RESPONSE_RECEIVED"
    assert len(responses) == 1
    assert responses[0].input_tokens == input_tokens
    assert responses[0].output_tokens == output_tokens
    assert responses[0].cost_amount == cost
    assert responses[0].cost_currency == currency


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "finish_reason", "failure_code"),
    [
        ("not JSON\x00", "stop", "SUMMARY_VALIDATION_FAILED"),
        ('{"status":"OK","status":"OK"}', "stop", "SUMMARY_VALIDATION_FAILED"),
        (b"\xff", "stop", "SUMMARY_VALIDATION_FAILED"),
        ('{"status":"OK"}', "length", "SUMMARY_PROVIDER_INCOMPLETE"),
        ('{"status":"OK"}', None, "SUMMARY_PROVIDER_INCOMPLETE"),
    ],
)
async def test_unusable_provider_output_is_stored_then_rejected_without_losing_cost(
    factory: async_sessionmaker[AsyncSession], payload, finish_reason, failure_code
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    await _commit_intent(service, key, lease_token, tenant=tenant)
    stored = await service.store_provider_response(
        key,
        lease_token=lease_token,
        response=SummaryResponseData(
            provider="offline",
            model="offline-summary-service",
            raw_response=payload,
            usage_metadata={},
            input_tokens=700,
            output_tokens=300,
            total_tokens=1000,
            finish_reason=finish_reason,
        ),
    )
    job, dispatch, responses = await _job_row(factory, key)
    assert job.status == "RESPONSE_STORED"
    assert dispatch.outcome == "RESPONSE_RECEIVED"
    assert dispatch.reserved_cost_amount > 0
    assert responses[0].id == stored.response_id
    expected = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    assert base64.b64decode(responses[0].raw_response["data"]) == expected

    recovery = await service.claim_recovery(key)
    assert recovery is not None
    settlement = await service.validate_stored_response(key, lease_token=recovery.lease_token)
    job, dispatch, responses = await _job_row(factory, key)
    assert settlement.status == "FAILED"
    assert job.failure_code == failure_code
    assert settlement.artifact_id is None
    assert dispatch.outcome == "RESPONSE_RECEIVED"
    assert dispatch.reserved_cost_amount > 0
    assert len(responses) == 1


@pytest.mark.asyncio
async def test_unexpected_provider_model_is_recorded_but_cannot_be_published(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    await _commit_intent(service, key, lease_token, tenant=tenant)
    await service.store_provider_response(
        key,
        lease_token=lease_token,
        response=SummaryResponseData(
            provider="unexpected-provider",
            model="unexpected-model",
            raw_response=_candidate(tenant),
            usage_metadata={"cost_status": "UNKNOWN_FOR_RETURNED_MODEL"},
            input_tokens=700,
            output_tokens=300,
            total_tokens=1000,
            finish_reason="stop",
        ),
    )
    recovery = await service.claim_recovery(key)
    assert recovery is not None
    settlement = await service.validate_stored_response(key, lease_token=recovery.lease_token)
    job, dispatch, responses = await _job_row(factory, key)
    assert settlement.status == "FAILED"
    assert job.failure_code == "SUMMARY_RESPONSE_MODEL_MISMATCH"
    assert settlement.artifact_id is None
    assert dispatch.outcome == "RESPONSE_RECEIVED"
    assert responses[0].model == "unexpected-model"
    assert responses[0].provider == "unexpected-provider"
    assert responses[0].cost_amount is None


@pytest.mark.asyncio
async def test_unknown_frozen_token_counter_cannot_dispatch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    verifier = _Verifier(lambda binding: _validation_context(tenant))
    service = SummaryJobService(factory, verifier=verifier)
    created = await service.create_job(
        replace(_new_summary_job(tenant), tokenizer_id="unknown-counter")
    )
    claimed = await service.claim(created.key)
    assert claimed is not None
    with pytest.raises(SummaryServiceError) as unsupported:
        await service.commit_dispatch_intent(
            created.key,
            lease_token=claimed.lease_token,
            pricing_snapshot_id=tenant.pricing_id,
            reserved_input_tokens=700,
            reserved_output_tokens=300,
        )
    assert unsupported.value.code == "SUMMARY_COUNTER_UNSUPPORTED"
    async with factory() as session:
        job = await session.get(ThreadSummaryJob, created.key.job_id)
        count = await session.scalar(
            select(func.count())
            .select_from(ThreadSummaryDispatch)
            .where(ThreadSummaryDispatch.job_id == created.key.job_id)
        )
    assert job is not None and job.status == "CLAIMED"
    assert count == 0 and not job.budget_reserved


@pytest.mark.asyncio
async def test_published_newer_parent_revision_supersedes_pending_child(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    (
        service,
        first_key,
        first_lease,
        second_source,
        _,
    ) = await _validated_two_source_with_pending_artifact(
        factory,
        tenant,
    )
    first_published = await service.publish(
        first_key,
        lease_token=first_lease,
        published_by="summary-publisher-v1",
    )
    assert first_published.status == "PUBLISHED"
    parent_artifact = (await _artifact_rows(factory, first_key))[0]

    child_command = NewSummaryJob(
        workspace_id=tenant.workspace_id,
        thread_id=tenant.thread_id,
        trigger_run_id=second_source.run_id,
        source_watermark=3,
        source_scope={
            "kind": "COMPLETED_PREFIX",
            "through_sequence": 2,
            "before_run_id": str(second_source.run_id),
        },
        parent_artifact_id=parent_artifact.id,
        parent_revision_hash=parent_artifact.revision_hash,
        policy_spec={"schema_version": 1, "mode": "single_dispatch", "variant": "child"},
        policy_version="summary-policy-v1",
        prompt_version="summary_candidate_zh_v1",
        budget_version="summary-budget-v1",
        model_spec={"provider": "offline", "model": "offline-summary-service"},
        tokenizer_id="approx_language_v1",
        tokenizer_version="1",
        target_budget_tokens=1500,
        permission_snapshot_hash=_hash(tenant.permission_snapshot),
        created_by=tenant.user_id,
        sources=(tenant.source, second_source),
    )
    child_verifier = _Verifier(lambda binding: _two_source_context(tenant, second_source))
    child_service = SummaryJobService(
        factory, verifier=child_verifier, budget_reserver=_TestBudgetReserver()
    )
    child = await child_service.create_job(child_command)
    child_claim = await child_service.claim(child.key)
    assert child_claim is not None
    await _commit_intent(child_service, child.key, child_claim.lease_token, tenant=tenant)
    await child_service.store_provider_response(
        child.key,
        lease_token=child_claim.lease_token,
        response=SummaryResponseData(
            provider="offline",
            model="offline-summary-service",
            raw_response=_two_source_candidate(tenant, second_source),
            usage_metadata={"source": "offline"},
            input_tokens=700,
            output_tokens=300,
            total_tokens=1000,
            finish_reason="stop",
        ),
    )
    child_recovery = await child_service.claim_recovery(child.key)
    assert child_recovery is not None
    child_settled = await child_service.validate_stored_response(
        child.key,
        lease_token=child_recovery.lease_token,
    )
    assert child_settled.status == "VALIDATED", child_settled.validation_result

    newer_verifier = _Verifier(lambda binding: _two_source_context(tenant, second_source))
    newer_service = SummaryJobService(
        factory, verifier=newer_verifier, budget_reserver=_TestBudgetReserver()
    )
    newer = await newer_service.create_job(
        NewSummaryJob(
            workspace_id=tenant.workspace_id,
            thread_id=tenant.thread_id,
            trigger_run_id=second_source.run_id,
            source_watermark=3,
            source_scope={
                "kind": "COMPLETED_PREFIX",
                "through_sequence": 2,
                "before_run_id": str(second_source.run_id),
            },
            parent_artifact_id=parent_artifact.id,
            parent_revision_hash=parent_artifact.revision_hash,
            policy_spec={
                "schema_version": 1,
                "mode": "single_dispatch",
                "variant": "newer",
            },
            policy_version="summary-policy-v1",
            prompt_version="summary_candidate_zh_v1",
            budget_version="summary-budget-v1",
            model_spec={"provider": "offline", "model": "offline-summary-service"},
            tokenizer_id="approx_language_v1",
            tokenizer_version="1",
            target_budget_tokens=1500,
            permission_snapshot_hash=_hash(tenant.permission_snapshot),
            created_by=tenant.user_id,
            sources=(tenant.source, second_source),
        )
    )
    newer_claim = await newer_service.claim(newer.key)
    assert newer_claim is not None
    await _commit_intent(newer_service, newer.key, newer_claim.lease_token, tenant=tenant)
    await newer_service.store_provider_response(
        newer.key,
        lease_token=newer_claim.lease_token,
        response=SummaryResponseData(
            provider="offline",
            model="offline-summary-service",
            raw_response=_two_source_candidate(tenant, second_source),
            usage_metadata={"source": "offline"},
            input_tokens=700,
            output_tokens=300,
            total_tokens=1000,
            finish_reason="stop",
        ),
    )
    newer_recovery = await newer_service.claim_recovery(newer.key)
    assert newer_recovery is not None
    newer_settled = await newer_service.validate_stored_response(
        newer.key,
        lease_token=newer_recovery.lease_token,
    )
    assert newer_settled.status == "VALIDATED"
    newer_published = await newer_service.publish(
        newer.key,
        lease_token=newer_recovery.lease_token,
        published_by="summary-publisher-v2",
    )
    assert newer_published.status == "PUBLISHED"

    superseded = await child_service.publish(
        child.key,
        lease_token=child_recovery.lease_token,
        published_by="summary-publisher-child",
    )
    assert superseded.status == "STALE"
    child_job, child_dispatch, child_responses = await _job_row(factory, child.key)
    child_artifacts = await _artifact_rows(factory, child.key)
    assert child_job.failure_code == "SUMMARY_PARENT_REVISION_SUPERSEDED"
    assert child_dispatch.outcome == "RESPONSE_RECEIVED"
    assert child_dispatch.reserved_cost_amount > 0
    assert len(child_responses) == 1
    assert child_artifacts[0].publication_status == "STALE"


@pytest.mark.asyncio
async def test_concurrent_root_publications_choose_one_current_thread_revision(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    _, first_key, first_lease, _ = await _validated_with_pending_artifact(factory, tenant)
    _, second_key, second_lease, _, _ = await _validated_two_source_with_pending_artifact(
        factory, tenant
    )

    class DelayedPublicationVerifier:
        async def verify(self, session, binding):
            del session
            assert binding.phase is SummaryVerifierPhase.PUBLICATION
            await asyncio.sleep(0.1)
            return SummaryTrustDecision.verified()

    first_service = SummaryJobService(factory, verifier=DelayedPublicationVerifier())
    second_service = SummaryJobService(factory, verifier=DelayedPublicationVerifier())
    first, second = await asyncio.gather(
        first_service.publish(first_key, lease_token=first_lease, published_by="first"),
        second_service.publish(second_key, lease_token=second_lease, published_by="second"),
    )
    assert sorted((first.status, second.status)) == ["PUBLISHED", "STALE"]
    async with factory() as session:
        statuses = tuple(
            await session.scalars(
                select(ThreadSummaryArtifact.publication_status).where(
                    ThreadSummaryArtifact.workspace_id == tenant.workspace_id,
                    ThreadSummaryArtifact.thread_id == tenant.thread_id,
                )
            )
        )
    assert sorted(statuses) == ["PUBLISHED", "STALE"]


@pytest.mark.asyncio
async def test_two_workers_publish_once_and_losing_owner_cannot_change_published_state(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token, _ = await _validated_with_pending_artifact(factory, tenant)

    first, second = await asyncio.gather(
        service.publish(
            key,
            lease_token=lease_token,
            published_by="worker-a",
        ),
        service.publish(
            key,
            lease_token=lease_token,
            published_by="worker-b",
        ),
    )
    assert first.status == second.status == "PUBLISHED"
    artifacts = await _artifact_rows(factory, key)
    _, dispatch, _ = await _job_row(factory, key)
    assert len(artifacts) == 1
    assert artifacts[0].publication_status == "PUBLISHED"
    assert artifacts[0].published_by in {"worker-a", "worker-b"}
    assert dispatch.outcome == "RESPONSE_RECEIVED"
    assert dispatch.reserved_cost_amount == Decimal("0.00162500")

    with pytest.raises(SummaryServiceError) as late_owner:
        await service.store_provider_response(
            key,
            lease_token=lease_token,
            response=SummaryResponseData(
                provider="offline",
                model="offline-summary-service",
                raw_response=_candidate(tenant),
                usage_metadata={},
            ),
        )
    assert late_owner.value.code == "SUMMARY_RESPONSE_STATE_INVALID"
    assert len((await _job_row(factory, key))[2]) == 1

    replay = await service.publish(
        key,
        lease_token=lease_token,
        published_by="worker-c",
        now=datetime.now(UTC) + timedelta(hours=1),
    )
    assert replay.status == "PUBLISHED"
    assert replay.artifact_id == first.artifact_id


@pytest.mark.asyncio
async def test_permission_revocation_retains_response_and_cost(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    gate = {"deny_publication": False}

    class RevokingVerifier:
        async def verify(
            self,
            session: AsyncSession,
            binding: SummaryJobBinding,
        ) -> SummaryTrustDecision:
            del session
            if binding.phase is SummaryVerifierPhase.PUBLICATION:
                gate["deny_publication"] = True
                return SummaryTrustDecision.denied("PERMISSION_REVOKED")
            return SummaryTrustDecision.verified(_validation_context(tenant))

    service, key, lease_token, _ = await _validated_with_pending_artifact(
        factory,
        tenant,
        verifier=RevokingVerifier(),
    )
    outcome = await service.publish(
        key,
        lease_token=lease_token,
        published_by="summary-publisher-v1",
    )
    assert outcome.status == "STALE"
    job, dispatch, responses = await _job_row(factory, key)
    artifacts = await _artifact_rows(factory, key)
    assert job.status == "STALE"
    assert job.failure_code == "PERMISSION_REVOKED"
    assert dispatch.outcome == "RESPONSE_RECEIVED"
    assert dispatch.reserved_cost_amount > 0
    assert len(responses) == 1
    assert artifacts[0].publication_status == "STALE"


@pytest.mark.asyncio
async def test_parent_revision_no_longer_current_cas_fails_closed_and_retains_evidence(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token, response_id = await _validated_with_pending_artifact(factory, tenant)
    artifact = (await _artifact_rows(factory, key))[0]
    async with factory() as session:
        async with session.begin():
            job = await session.get(ThreadSummaryJob, key.job_id, with_for_update=True)
            assert job is not None
            job.parent_artifact_id = artifact.id
            job.parent_revision_hash = artifact.revision_hash

    outcome = await service.publish(
        key,
        lease_token=lease_token,
        published_by="summary-publisher-v1",
    )
    assert outcome.status == "STALE"
    job, dispatch, responses = await _job_row(factory, key)
    assert job.failure_code == "SUMMARY_PARENT_REVISION_INVALID"
    assert dispatch.outcome == "RESPONSE_RECEIVED"
    assert len(responses) == 1
    async with factory() as session:
        artifact_after = await session.get(ThreadSummaryArtifact, artifact.id)
    assert artifact_after is not None
    assert artifact_after.publication_status == "STALE"


@pytest.mark.asyncio
async def test_default_verifier_fails_closed_and_can_resume_after_trusted_verifier_restart(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    await _commit_intent(service, key, lease_token, tenant=tenant)
    await _store_response(service, key, lease_token, tenant)

    default_service = SummaryJobService(factory)
    recovery = await default_service.claim_recovery(key)
    assert recovery is not None
    with pytest.raises(SummaryServiceError) as unavailable:
        await default_service.validate_stored_response(
            key,
            lease_token=recovery.lease_token,
        )
    assert unavailable.value.code == "SUMMARY_TRUST_UNAVAILABLE"
    job, _, responses = await _job_row(factory, key)
    assert job.status == "RESPONSE_STORED"
    assert len(responses) == 1

    trusted = SummaryJobService(
        factory,
        verifier=_Verifier(lambda binding: _validation_context(tenant)),
        clock=lambda: datetime.now(UTC) + timedelta(hours=1),
    )
    recovery = await trusted.claim_recovery(key)
    assert recovery is not None
    settled = await trusted.validate_stored_response(
        key,
        lease_token=recovery.lease_token,
    )
    assert settled.status == "VALIDATED"
    assert len((await _job_row(factory, key))[2]) == 1


@pytest.mark.asyncio
async def test_failed_intent_does_not_exist_for_queued_jobs_and_failure_is_only_pre_dispatch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant(factory)
    service, key, lease_token = await _create_claimed(factory, tenant)
    failed = await service.fail_before_dispatch(
        key,
        lease_token=lease_token,
        failure_code="SUMMARY_LOCAL_PREFLIGHT_FAILED",
    )
    assert failed.status == "FAILED"
    async with factory() as session:
        job = await session.get(ThreadSummaryJob, key.job_id)
    assert job is not None
    assert job.budget_reserved is False
    assert job.dispatch_id is None
    async with factory() as session:
        actual = await session.scalar(
            select(func.count())
            .select_from(ThreadSummaryDispatch)
            .where(
                ThreadSummaryDispatch.workspace_id == key.workspace_id,
                ThreadSummaryDispatch.thread_id == key.thread_id,
                ThreadSummaryDispatch.job_id == key.job_id,
            )
        )
    assert actual == 0
    async with factory() as session:
        response_count = await session.scalar(
            select(func.count())
            .select_from(ThreadSummaryResponse)
            .where(
                ThreadSummaryResponse.workspace_id == key.workspace_id,
                ThreadSummaryResponse.thread_id == key.thread_id,
                ThreadSummaryResponse.job_id == key.job_id,
            )
        )
    assert response_count == 0
