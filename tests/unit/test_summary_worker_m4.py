"""The optional summary worker never retries or bypasses the frozen request."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from packages.model_gateway.contracts import CostEstimate, ModelResponse, ModelToolCall, ModelUsage
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

    async def renew_lease(self, key, **kwargs):
        self.events.append("renew")
        assert key == self.key and kwargs["lease_token"] == "lease"
        self.lease_seconds = kwargs["lease_seconds"]

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
    assert events == [
        "prepare", "renew", "commit", "mark", "gateway", "store", "recovery", "validate"
    ]
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
    assert events == ["prepare", "renew", "commit", "mark", "gateway", "unknown"]
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_lease_covers_frozen_provider_timeout_before_dispatch(setup_worker):
    claim, context, spec, service, gateway, _ = setup_worker
    primary = replace(spec.model_plan.primary, timeout_seconds=Decimal("300.001"))
    spec = replace(spec, model_plan=replace(spec.model_plan, primary=primary))
    service.profile = primary
    service.spec = frozen_summary_model_spec(primary)
    await SummaryWorker(service, gateway).run_claimed(
        claimed=claim, context=context, frozen_spec=spec, pricing_snapshot_id=uuid4()
    )
    assert service.lease_seconds >= 301 + 180
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_failed_lease_renewal_prevents_reservation_and_dispatch(setup_worker):
    claim, context, spec, service, gateway, events = setup_worker

    async def lost_lease(*_args, **_kwargs):
        raise SummaryServiceError("SUMMARY_LEASE_LOST", "lease lost")

    service.renew_lease = lost_lease
    with pytest.raises(SummaryServiceError, match="lease lost"):
        await SummaryWorker(service, gateway).run_claimed(
            claimed=claim, context=context, frozen_spec=spec, pricing_snapshot_id=uuid4()
        )
    assert events == ["prepare"] and not gateway.calls


@pytest.mark.asyncio
async def test_provider_timeout_is_unknown_without_retry(setup_worker):
    claim, context, spec, service, gateway, events = setup_worker
    primary = replace(spec.model_plan.primary, timeout_seconds=Decimal("0.001"))
    spec = replace(spec, model_plan=replace(spec.model_plan, primary=primary))
    service.profile = primary
    service.spec = frozen_summary_model_spec(primary)

    async def wait_forever(*_args):
        events.append("gateway")
        await asyncio.Event().wait()

    gateway.generate_resolved = wait_forever
    result = await SummaryWorker(service, gateway).run_claimed(
        claimed=claim, context=context, frozen_spec=spec, pricing_snapshot_id=uuid4()
    )
    assert result.status == "UNKNOWN_OUTCOME"
    assert events == ["prepare", "renew", "commit", "mark", "gateway", "unknown"]


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_preserves_unknown_outcome(setup_worker):
    claim, context, spec, service, gateway, events = setup_worker
    started = asyncio.Event()

    async def wait_forever(*_args):
        events.append("gateway")
        started.set()
        await asyncio.Event().wait()

    gateway.generate_resolved = wait_forever
    task = asyncio.create_task(
        SummaryWorker(service, gateway).run_claimed(
            claimed=claim, context=context, frozen_spec=spec, pricing_snapshot_id=uuid4()
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["prepare", "renew", "commit", "mark", "gateway", "unknown"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [Decimal("NaN"), Decimal("Infinity"), Decimal("100000")])
async def test_invalid_model_timeout_stops_before_preparation(setup_worker, timeout):
    claim, context, spec, service, gateway, events = setup_worker
    primary = replace(spec.model_plan.primary, timeout_seconds=timeout)
    spec = replace(spec, model_plan=replace(spec.model_plan, primary=primary))
    with pytest.raises(SummaryServiceError) as failure:
        await SummaryWorker(service, gateway).run_claimed(
            claimed=claim, context=context, frozen_spec=spec, pricing_snapshot_id=uuid4()
        )
    assert failure.value.code == "SUMMARY_WORKER_POLICY_INVALID"
    assert events == [] and not gateway.calls


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


@pytest.mark.parametrize(
    "cost",
    [
        CostEstimate(Decimal("0.01"), "ééé"),
        CostEstimate(Decimal("1000000000000"), "USD"),
        CostEstimate(Decimal("0.000000001"), "USD"),
    ],
)
def test_unpersistable_provider_cost_retains_response_and_marks_cost_invalid(cost):
    response = ModelResponse(
        content='{"status":"OK"}',
        provider="offline",
        model="m",
        finish_reason="stop",
        usage=ModelUsage(input_tokens=12, output_tokens=3, total_tokens=15),
        cost_estimate=cost,
    )
    stored = _response_data(response)
    assert stored.raw_response == response.content
    assert stored.input_tokens == 12 and stored.output_tokens == 3
    assert stored.cost_amount is None and stored.cost_currency is None
    assert stored.usage_metadata["cost_status"] == "INVALID_REPORTED"


def test_unpersistable_usage_retains_response_and_marks_usage_unknown():
    response = ModelResponse(
        content='{"status":"OK"}',
        provider="offline",
        model="m",
        usage=ModelUsage(input_tokens=2_147_483_648, output_tokens=1, total_tokens=2_147_483_649),
    )
    stored = _response_data(response)
    assert stored.raw_response == response.content
    assert stored.input_tokens is None and stored.output_tokens is None
    assert stored.usage_metadata["usage_status"] == "UNKNOWN"


def test_persistable_cost_remains_available_for_accounting():
    response = ModelResponse(
        content="{}",
        provider="offline",
        model="m",
        cost_estimate=CostEstimate(Decimal("0.00000001"), "USD"),
    )
    stored = _response_data(response)
    assert stored.cost_amount == Decimal("0.00000001")
    assert stored.cost_currency == "USD"
    assert stored.usage_metadata["cost_status"] == "ESTIMATED"
