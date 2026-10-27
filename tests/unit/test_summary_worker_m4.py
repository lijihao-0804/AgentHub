"""The optional summary worker never retries or bypasses the frozen request."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from packages.agent_runtime.summary_prompt import SummaryPrompt
from packages.agent_runtime.summary_service import (
    ClaimedSummaryJob,
    PreparedSummaryDispatch,
    SummaryJobKey,
    SummaryServiceError,
    SummarySettlement,
)
from packages.agent_runtime.summary_worker import (
    SummaryWorker,
    _response_data,
    frozen_summary_model_spec,
)
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.contracts import ModelResponse, ModelToolCall, ModelUsage
from tests.unit.test_context_policy_m2_runtime import _freeze, _profile


def _summary_policy():
    return {
        "enabled": True,
        "policy_version": "thread_summary_policy_v1",
        "prompt_version": "summary_candidate_zh_v1",
        "validator_version": "summary_mechanical_v1",
        "tokenizer_id": "approx_language_v1",
        "tokenizer_version": 1,
        "soft_trigger_percent": 70,
        "hard_trigger_percent": 85,
        "target_percent": 55,
    }


class FakeService:
    def __init__(self, key, actor, profile, events):
        self.key = key
        self.actor = actor
        self.profile = profile
        self.events = events
        self.spec = frozen_summary_model_spec(profile)
        self.stored = None

    async def prepare_dispatch(self, key, **kwargs):
        self.events.append("prepare")
        assert key == self.key
        assert kwargs["reserved_input_tokens"] > 0
        assert kwargs["reserved_output_tokens"] == self.profile.max_tokens
        return PreparedSummaryDispatch(
            key,
            self.actor,
            "thread_summary_policy_v1",
            "summary_candidate_zh_v1",
            "approx_language_v1",
            "1",
            SummaryPrompt("frozen system", "frozen user", "summary_candidate_zh_v1", 1, 1),
            {"request": "verified"},
            self.spec,
        )

    async def commit_dispatch_intent(self, key, **kwargs):
        self.events.append("commit")
        assert key == self.key and kwargs["request_spec"] == {"request": "verified"}

    async def mark_dispatched(self, key, **_kwargs):
        self.events.append("mark")
        assert key == self.key

    async def store_provider_response(self, key, **kwargs):
        self.events.append("store")
        assert key == self.key
        self.stored = kwargs["response"]

    async def mark_dispatch_unknown(self, key, **_kwargs):
        self.events.append("unknown")
        return SummarySettlement(key, "UNKNOWN_OUTCOME", None, None, None)

    async def claim_recovery(self, key):
        self.events.append("recovery")
        return ClaimedSummaryJob(key, "local-recovery", datetime.now(UTC) + timedelta(minutes=1))

    async def validate_stored_response(self, key, **_kwargs):
        self.events.append("validate")
        return SummarySettlement(key, "VALIDATED", uuid4(), uuid4(), {"valid": True})


class FakeGateway:
    def __init__(self, events, *, fail=False):
        self.events = events
        self.fail = fail
        self.calls = []

    async def generate_resolved(self, context, plan, request):
        self.events.append("gateway")
        self.calls.append((context, plan, request))
        if self.fail:
            raise TimeoutError("provider may have received request")
        return ModelResponse(
            content='{"status":"OK"}',
            provider=plan.primary.provider,
            model=plan.primary.model,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=500, output_tokens=100, total_tokens=600),
        )


@pytest.fixture
def setup_worker():
    workspace, actor = uuid4(), uuid4()
    primary = _profile(total=32768, output=2048, workspace=workspace)
    fallback = _profile(total=32768, output=2048, workspace=workspace)
    spec = _freeze((primary, fallback))
    spec = replace(spec, runtime={**spec.runtime, "summary_policy": _summary_policy()})
    key = SummaryJobKey(workspace, uuid4(), uuid4())
    claim = ClaimedSummaryJob(key, "lease", datetime.now(UTC) + timedelta(minutes=1))
    context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="request", trace_id="trace", user_id=str(actor)),
            organization_id=str(uuid4()),
            org_role="owner",
        ),
        workspace_id=str(workspace),
    )
    events = []
    service = FakeService(key, actor, spec.model_plan.primary, events)
    gateway = FakeGateway(events)
    return claim, context, spec, service, gateway, events


@pytest.mark.asyncio
async def test_worker_uses_one_admitted_frozen_model_call_and_persists_before_validation(
    setup_worker,
):
    claim, context, spec, service, gateway, events = setup_worker
    result = await SummaryWorker(service, gateway).run_claimed(
        claimed=claim, context=context, frozen_spec=spec, pricing_snapshot_id=uuid4()
    )
    assert result.status == "VALIDATED"
    assert events == ["prepare", "commit", "mark", "gateway", "store", "recovery", "validate"]
    assert len(gateway.calls) == 1
    _, plan, request = gateway.calls[0]
    assert plan.primary == spec.model_plan.primary and not plan.fallbacks
    assert len(spec.model_plan.fallbacks) == 1
    assert request.retry_policy.max_attempts == plan.retry_policy.max_attempts == 1
    assert not request.tools
    assert [message.content for message in request.messages] == ["frozen system", "frozen user"]
    assert service.stored.raw_response == '{"status":"OK"}'
    assert service.stored.finish_reason == "stop"


@pytest.mark.asyncio
async def test_uncertain_provider_failure_marks_unknown_without_retry_or_fallback(setup_worker):
    claim, context, spec, service, gateway, events = setup_worker
    gateway.fail = True
    result = await SummaryWorker(service, gateway).run_claimed(
        claimed=claim, context=context, frozen_spec=spec, pricing_snapshot_id=uuid4()
    )
    assert result.status == "UNKNOWN_OUTCOME"
    assert events == ["prepare", "commit", "mark", "gateway", "unknown"]
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_model_identity_mismatch_prevents_reservation_and_dispatch(setup_worker):
    claim, context, spec, service, gateway, events = setup_worker
    service.spec = {**service.spec, "credential_ref": str(uuid4())}
    with pytest.raises(SummaryServiceError) as failure:
        await SummaryWorker(service, gateway).run_claimed(
            claimed=claim, context=context, frozen_spec=spec, pricing_snapshot_id=uuid4()
        )
    assert failure.value.code == "SUMMARY_WORKER_MODEL_MISMATCH"
    assert events == ["prepare"] and not gateway.calls


@pytest.mark.asyncio
async def test_unrecognized_frozen_summary_policy_stops_before_preparation(setup_worker):
    claim, context, spec, service, gateway, events = setup_worker
    bad_policy = {**spec.runtime["summary_policy"], "target_percent": 56}
    spec = replace(spec, runtime={**spec.runtime, "summary_policy": bad_policy})
    with pytest.raises(SummaryServiceError) as failure:
        await SummaryWorker(service, gateway).run_claimed(
            claimed=claim, context=context, frozen_spec=spec, pricing_snapshot_id=uuid4()
        )
    assert failure.value.code == "SUMMARY_WORKER_DISABLED"
    assert events == [] and not gateway.calls


def test_unexpected_tool_calls_are_preserved_as_nonpublishable_response():
    response = ModelResponse(
        content="{}",
        provider="offline",
        model="m",
        finish_reason="stop",
        tool_calls=(ModelToolCall(name="unexpected", arguments={}),),
    )
    stored = _response_data(response)
    assert stored.raw_response == "{}"
    assert stored.finish_reason == "unexpected_tool_calls"
    assert stored.usage_metadata["unexpected_tool_call_count"] == 1
