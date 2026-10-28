from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.frozen import (
    SUMMARY_NON_THINKING_COUNTER_ID,
    SUMMARY_NON_THINKING_COUNTER_REVISION,
    SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
    SUMMARY_NON_THINKING_MODE,
    SUMMARY_NON_THINKING_MODEL,
    SUMMARY_NON_THINKING_PROVIDER,
    SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
    SUMMARY_NON_THINKING_STRATEGY_VERSION,
    SUMMARY_NON_THINKING_TOKENIZER_SHA256,
)
from packages.agent_runtime.summary_models import ThreadSummaryJob
from packages.agent_runtime.summary_service import (
    SummaryJobKey,
    SummaryJobService,
    SummaryServiceError,
    SummaryTrustStatus,
)
from packages.agent_runtime.summary_state import (
    SummaryJobStatus,
    SummaryState,
    claim,
    commit_dispatch_intent,
    store_response,
    validate_response,
)
from packages.agent_runtime.summary_state import (
    publish as publish_state,
)


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session(_AsyncContext):
    def __init__(self) -> None:
        self.flush_count = 0
        self.execute_count = 0

    def begin(self) -> _AsyncContext:
        return _AsyncContext()

    async def flush(self) -> None:
        self.flush_count += 1

    async def execute(self, _statement):
        self.execute_count += 1
        return SimpleNamespace(rowcount=1)


class _SessionFactory:
    def __init__(self) -> None:
        self.session = _Session()

    def __call__(self) -> _Session:
        return self.session


def _non_thinking_identity() -> dict[str, object]:
    return {
        "request_strategy_version": SUMMARY_NON_THINKING_STRATEGY_VERSION,
        "request_mode": SUMMARY_NON_THINKING_MODE,
        "request_route_allowlist": SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
        "request_counter_id": SUMMARY_NON_THINKING_COUNTER_ID,
        "request_counter_revision": SUMMARY_NON_THINKING_COUNTER_REVISION,
        "request_counter_runtime_version": SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
        "request_counter_sha256": SUMMARY_NON_THINKING_TOKENIZER_SHA256,
        "request_counter_thinking": False,
    }


def _validated_v2_job(*, now: datetime) -> ThreadSummaryJob:
    lease_token = "recovery-lease"
    state = claim(SummaryState(), lease_token=lease_token)
    state = commit_dispatch_intent(state, lease_token=lease_token)
    state = store_response(state, lease_token=lease_token, response_ref=str(uuid4()))
    state = validate_response(state, lease_token=lease_token, valid=True)
    assert state.status is SummaryJobStatus.VALIDATED

    identity = _non_thinking_identity()
    workspace_id = uuid4()
    thread_id = uuid4()
    job_id = uuid4()
    return ThreadSummaryJob(
        id=job_id,
        workspace_id=workspace_id,
        thread_id=thread_id,
        trigger_run_id=uuid4(),
        model_round=1,
        identity_hash="1" * 64,
        source_watermark=2,
        source_scope={},
        source_scope_hash="2" * 64,
        source_content_hash="3" * 64,
        parent_artifact_id=None,
        parent_revision_hash=None,
        policy_spec={
            "enabled": True,
            "policy_version": "thread_summary_policy_v1",
            "prompt_version": "summary_candidate_zh_v1",
            "validator_version": "summary_mechanical_v1",
            "tokenizer_id": "approx_language_v1",
            "tokenizer_version": 1,
            "soft_trigger_percent": 70,
            "hard_trigger_percent": 85,
            "target_percent": 55,
            **identity,
        },
        policy_version="thread_summary_policy_v1",
        prompt_version="summary_candidate_zh_v1",
        budget_version="summary-budget-v1",
        model_spec={
            "provider": SUMMARY_NON_THINKING_PROVIDER,
            "model": SUMMARY_NON_THINKING_MODEL,
            **identity,
        },
        model_spec_hash="4" * 64,
        tokenizer_id="approx_language_v1",
        tokenizer_version="1",
        target_budget_tokens=512,
        permission_snapshot_hash="5" * 64,
        status=state.status.value,
        lease_token=state.lease_token,
        lease_expires_at=now + timedelta(minutes=5),
        budget_reserved=state.budget_reserved,
        dispatch_id=uuid4(),
        response_id=uuid4(),
        artifact_id=uuid4(),
        created_by=uuid4(),
        validation_result={"valid": True},
    )


@pytest.mark.asyncio
async def test_publish_rejects_a_real_validated_v2_job_without_counter_verification():
    now = datetime.now(UTC)
    job = _validated_v2_job(now=now)
    factory = _SessionFactory()
    service = SummaryJobService(factory, clock=lambda: now)
    checkpoint_checks = []

    async def lock_job(_session, key):
        assert key == SummaryJobKey(job.workspace_id, job.thread_id, job.id)
        return job

    async def verify_checkpoint(_job):
        checkpoint_checks.append(True)

    service._lock_job = lock_job
    service._verify_fixed_job_checkpoint = verify_checkpoint

    with pytest.raises(SummaryServiceError) as failure:
        await service.publish(
            SummaryJobKey(job.workspace_id, job.thread_id, job.id),
            lease_token=job.lease_token,
            published_by="test-recovery",
            now=now,
        )

    assert failure.value.code == "SUMMARY_REQUEST_COUNTER_UNAVAILABLE"
    assert job.status == SummaryJobStatus.VALIDATED.value
    assert job.artifact_id is not None
    assert factory.session.flush_count == 0
    assert factory.session.execute_count == 0
    assert checkpoint_checks == []


@pytest.mark.asyncio
async def test_publish_does_not_report_an_already_published_v2_job_as_consumable():
    now = datetime.now(UTC)
    job = _validated_v2_job(now=now)
    state = SummaryState(
        status=SummaryJobStatus.VALIDATED,
        lease_token=job.lease_token,
        budget_reserved=True,
        response_ref=str(job.response_id),
    )
    state = publish_state(
        state,
        lease_token=job.lease_token,
        revision_ref=str(job.artifact_id),
        bindings_current=True,
    )
    job.status = state.status.value
    factory = _SessionFactory()
    service = SummaryJobService(factory, clock=lambda: now)

    async def lock_job(_session, key):
        assert key == SummaryJobKey(job.workspace_id, job.thread_id, job.id)
        return job

    service._lock_job = lock_job

    with pytest.raises(SummaryServiceError) as failure:
        await service.publish(
            SummaryJobKey(job.workspace_id, job.thread_id, job.id),
            lease_token="stale-retry-token",
            published_by="test-recovery",
            now=now,
        )

    assert failure.value.code == "SUMMARY_REQUEST_COUNTER_UNAVAILABLE"
    assert job.status == SummaryJobStatus.PUBLISHED.value
    assert factory.session.flush_count == 0
    assert factory.session.execute_count == 0


@pytest.mark.asyncio
async def test_publish_still_accepts_provider_default_validated_recovery():
    now = datetime.now(UTC)
    job = _validated_v2_job(now=now)
    job.policy_spec = {
        "enabled": True,
        "policy_version": "thread_summary_policy_v1",
        "prompt_version": "summary_candidate_zh_v1",
        "validator_version": "summary_mechanical_v1",
        "tokenizer_id": "approx_language_v1",
        "tokenizer_version": 1,
        "soft_trigger_percent": 70,
        "hard_trigger_percent": 85,
        "target_percent": 55,
        "request_strategy_version": "summary_request_strategy_v1",
        "request_mode": "provider_default",
    }
    job.model_spec = {"provider": "offline", "model": "summary-model"}
    factory = _SessionFactory()
    service = SummaryJobService(factory, clock=lambda: now)
    artifact = SimpleNamespace(
        id=job.artifact_id,
        publication_status="PENDING",
    )

    async def lock_job(_session, _key):
        return job

    async def allow_parent(_session, _job):
        return True, None

    async def verified(_session, _binding):
        return SimpleNamespace(status=SummaryTrustStatus.VERIFIED)

    async def lock_artifact(_session, _key, _artifact_id):
        return artifact

    async def no_op(*_args):
        return None

    service._lock_job = lock_job
    service._verify_fixed_job_checkpoint = no_op
    service._lock_summary_thread = no_op
    service._lock_artifact = lock_artifact
    service._parent_is_current = allow_parent
    service._verify = verified

    result = await service.publish(
        SummaryJobKey(job.workspace_id, job.thread_id, job.id),
        lease_token=job.lease_token,
        published_by="provider-default-recovery",
        now=now,
    )

    assert result.status == SummaryJobStatus.PUBLISHED.value
    assert job.status == SummaryJobStatus.PUBLISHED.value
    assert factory.session.execute_count == 2


@pytest.mark.asyncio
async def test_publish_keeps_published_provider_default_idempotent():
    now = datetime.now(UTC)
    job = _validated_v2_job(now=now)
    job.policy_spec = {
        "enabled": True,
        "policy_version": "thread_summary_policy_v1",
        "prompt_version": "summary_candidate_zh_v1",
        "validator_version": "summary_mechanical_v1",
        "tokenizer_id": "approx_language_v1",
        "tokenizer_version": 1,
        "soft_trigger_percent": 70,
        "hard_trigger_percent": 85,
        "target_percent": 55,
        "request_strategy_version": "summary_request_strategy_v1",
        "request_mode": "provider_default",
    }
    job.model_spec = {"provider": "offline", "model": "summary-model"}
    state = SummaryState(
        status=SummaryJobStatus.VALIDATED,
        lease_token=job.lease_token,
        budget_reserved=True,
        response_ref=str(job.response_id),
    )
    state = publish_state(
        state,
        lease_token=job.lease_token,
        revision_ref=str(job.artifact_id),
        bindings_current=True,
    )
    job.status = state.status.value
    factory = _SessionFactory()
    service = SummaryJobService(factory, clock=lambda: now)

    async def lock_job(_session, _key):
        return job

    service._lock_job = lock_job

    first = await service.publish(
        SummaryJobKey(job.workspace_id, job.thread_id, job.id),
        lease_token=job.lease_token,
        published_by="provider-default-recovery",
        now=now,
    )
    second = await service.publish(
        SummaryJobKey(job.workspace_id, job.thread_id, job.id),
        lease_token=job.lease_token,
        published_by="provider-default-recovery",
        now=now,
    )

    assert first == second
    assert first.status == SummaryJobStatus.PUBLISHED.value
    assert first.key == SummaryJobKey(job.workspace_id, job.thread_id, job.id)
    assert first.response_id == job.response_id
    assert first.artifact_id == job.artifact_id
    assert job.status == SummaryJobStatus.PUBLISHED.value
    assert factory.session.execute_count == 0
