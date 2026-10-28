"""Fault injection around the summary dispatch fence without PostgreSQL."""

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.agent_runtime.summary_prompt import SummaryPrompt
from packages.agent_runtime.summary_service import (
    ClaimedSummaryJob,
    PreparedSummaryDispatch,
    SummaryJobKey,
    SummarySettlement,
)
from packages.agent_runtime.summary_state import (
    SummaryJobStatus,
    SummaryState,
    SummaryTransitionError,
    claim,
    claim_recovery,
    commit_dispatch_intent,
    recover_expired_lease,
)
from packages.agent_runtime.summary_worker import SummaryWorker, frozen_summary_model_spec
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.contracts import ModelResponse, ModelUsage
from tests.unit.test_context_policy_m2_runtime import _freeze, _profile

_CLAIM_TOKEN = "summary-job-claim"
_RUN_OWNER_TOKEN = "run-owner-token"


class _FencedSummaryService:
    def __init__(self, key, actor_id, profile, *, after_fence):
        self.key = key
        self.actor_id = actor_id
        self.profile = profile
        self.model_spec = frozen_summary_model_spec(profile)
        self.request_spec = {"request_mode": "provider_default"}
        self.after_fence = after_fence
        self.events = []
        self.state = claim(SummaryState(), lease_token=_CLAIM_TOKEN)

    async def prepare_dispatch(self, key, **kwargs):
        self.events.append("prepare")
        assert key == self.key
        assert kwargs["reserved_input_tokens"] > 0
        assert kwargs["reserved_output_tokens"] == self.profile.max_tokens
        return PreparedSummaryDispatch(
            key,
            self.actor_id,
            "thread_summary_policy_v1",
            "summary_candidate_zh_v1",
            "approx_language_v1",
            "1",
            SummaryPrompt("frozen system", "frozen user", "summary_candidate_zh_v1", 1, 1),
            self.request_spec,
            self.model_spec,
        )

    async def renew_lease(self, key, **kwargs):
        self.events.append("renew")
        assert key == self.key and kwargs["lease_token"] == _CLAIM_TOKEN

    async def commit_dispatch_intent(self, key, **kwargs):
        self.events.append("intent")
        assert key == self.key and kwargs["lease_token"] == _CLAIM_TOKEN
        self.state = commit_dispatch_intent(self.state, lease_token=_CLAIM_TOKEN)

    async def mark_dispatched(self, key, **kwargs):
        self.events.append("fence")
        assert key == self.key and kwargs["lease_token"] == _CLAIM_TOKEN
        self.after_fence()

    async def mark_dispatch_unknown(self, key, **kwargs):
        self.events.append("unknown")
        assert key == self.key and kwargs["lease_token"] == _CLAIM_TOKEN
        self.state = recover_expired_lease(self.state)
        return SummarySettlement(key, SummaryJobStatus.UNKNOWN_OUTCOME.value, None, None, None)


class _NeverCalledGateway:
    def __init__(self):
        self.calls = []

    async def generate_resolved(self, context, plan, request):
        self.calls.append((context, plan, request))
        return ModelResponse(
            content="unexpected",
            provider=plan.primary.provider,
            model=plan.primary.model,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )


def _setup_fault_case(fault: str):
    workspace_id, actor_id = uuid4(), uuid4()
    primary_profile = _profile(total=32768, output=2048, workspace=workspace_id)
    fallback = _profile(total=32768, output=2048, workspace=workspace_id)
    spec = _freeze((primary_profile, fallback))
    primary = spec.model_plan.primary
    spec = replace(
        spec,
        runtime={
            **spec.runtime,
            "summary_policy": {
                "enabled": True,
                "policy_version": "thread_summary_policy_v1",
                "prompt_version": "summary_candidate_zh_v1",
                "validator_version": "summary_mechanical_v1",
                "tokenizer_id": "approx_language_v1",
                "tokenizer_version": 1,
                "soft_trigger_percent": 70,
                "hard_trigger_percent": 85,
                "target_percent": 55,
            },
        },
    )
    context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="request",
                trace_id="trace",
                user_id=str(actor_id),
            ),
            organization_id=str(uuid4()),
            org_role="owner",
        ),
        workspace_id=str(workspace_id),
        permissions=frozenset({"agent_run", "workspace_read"}),
    )
    current_context = {"value": context}

    async def resolve_context(_context, _run):
        return current_context["value"]

    graph_service = SimpleNamespace(_authoritative_run_context=resolve_context)
    run = SimpleNamespace(id=uuid4())
    graph = _AgentRunGraph(
        graph_service,
        context,
        run,
        execution_owner_token=_RUN_OWNER_TOKEN,
    )
    run_row = SimpleNamespace(
        status="RUNNING",
        execution_owner=_RUN_OWNER_TOKEN,
        execution_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    def after_fence():
        if fault == "owner_takeover":
            run_row.execution_owner = "replacement-owner-token"
        elif fault == "permission_revocation":
            current_context["value"] = context.model_copy(
                update={"permissions": frozenset({"workspace_read"})}
            )
        else:
            raise AssertionError(f"unknown fault: {fault}")

    key = SummaryJobKey(workspace_id, uuid4(), uuid4())
    service = _FencedSummaryService(key, actor_id, primary, after_fence=after_fence)
    gateway = _NeverCalledGateway()
    claimed = ClaimedSummaryJob(
        key,
        _CLAIM_TOKEN,
        datetime.now(UTC) + timedelta(minutes=1),
    )
    return graph, run_row, context, spec, service, gateway, claimed


def test_summary_node_uses_langgraph_recognized_config_annotation():
    annotation = inspect.signature(_AgentRunGraph.summary).parameters["config"].annotation
    assert annotation == "RunnableConfig"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "expected_code"),
    [
        ("owner_takeover", "RUN_EXECUTION_LEASE_LOST"),
        ("permission_revocation", "FORBIDDEN"),
    ],
)
async def test_post_fence_owner_loss_or_permission_revocation_is_unknown_without_provider_call(
    fault, expected_code
):
    graph, run_row, context, spec, service, gateway, claimed = _setup_fault_case(fault)

    async def recheck_dispatch_authority():
        await graph._require_current_summary_actor()
        await graph._assert_summary_run_lease(run_row=run_row)

    with pytest.raises(AgentHubError) as rejected:
        await SummaryWorker(service, gateway).run_claimed(
            claimed=claimed,
            context=context,
            frozen_spec=spec,
            pricing_snapshot_id=uuid4(),
            before_network_call=recheck_dispatch_authority,
        )

    assert rejected.value.code == expected_code
    assert service.events == ["prepare", "renew", "intent", "fence", "unknown"]
    assert service.state.status is SummaryJobStatus.UNKNOWN_OUTCOME
    assert service.state.budget_reserved is True
    assert gateway.calls == []

    with pytest.raises(SummaryTransitionError):
        claim(service.state, lease_token="replacement-summary-worker")
    with pytest.raises(SummaryTransitionError):
        claim_recovery(service.state, lease_token="replacement-summary-worker")
