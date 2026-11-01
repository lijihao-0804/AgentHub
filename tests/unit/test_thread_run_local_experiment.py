from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from benchmarks.evaluation.deepseek_v41_summary_experiment import (
    DEFAULT_TOKENIZER_PATH,
    DeepseekV41SummaryExperimentGate,
)
from benchmarks.evaluation.memory_trial_budget import (
    RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
)
from benchmarks.evaluation.thread_run_local_experiment import (
    _ORIGINAL_FIXTURE_USER_TEXT,
    ELIGIBLE_ASSISTANT_BACKGROUND,
    EXPERIMENT_DATABASE_NAME,
    EXPERIMENT_DATABASE_URL_ENV,
    EXPERIMENT_SCOPE_ID,
    FIRST_RUN_INPUT,
    LONG_FIXTURE_APPENDICES,
    NEXT_RUN_INPUT,
    OUTPUT_TOKEN_LIMIT,
    PLANNED_PROVIDER_DISPATCHES,
    RAG_API_KEY_ENV,
    RAG_BASE_URL_ENV,
    THREAD_FIXTURE,
    WORKING_INPUT_LIMIT,
    ThreadRunConfig,
    ThreadRunExperimentError,
    _assert_expected_dispatches,
    _assert_run_checkpoint_selector,
    _assert_summary_preflight_metrics,
    _DispatchRecorder,
    _RecordingPreparedGateway,
    _request_has_summary_view,
    build_dry_run_report,
    is_allowlisted_beijing_url,
    load_config,
    require_dispatch_acceptance,
    safe_database_identity,
)
from packages.agent_runtime.adapters.langgraph.checkpoint import LangGraphCheckpointAdapter
from packages.agent_runtime.local_summary_experiment import LocalSummaryExperimentPermit
from packages.model_gateway.contracts import (
    AliyunDeepSeekRequestMode,
    CapabilityRequirements,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
)

_NO_IO_PATH = Path(".")


def _config(tmp_path: Path, *, database_url: str | None = None) -> ThreadRunConfig:
    return ThreadRunConfig(
        env_file=tmp_path / ".env",
        ledger_path=tmp_path / "ledger.json",
        output_directory=tmp_path / "output",
        tokenizer_path=tmp_path / "tokenizer.model",
        database_url=database_url,
        base_url="https://dashscope.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        api_key="test-only-secret",
        credential_master_key_configured=True,
    )


def _ledger(*, attempts: int = 2) -> dict:
    rows = [
        {
            "charged_or_reserved_cny": "0.157658",
            "estimated_input_tokens": 32768,
            "requested_output_tokens": 512,
            "stage": "M4",
            "scope_id": EXPERIMENT_SCOPE_ID,
        }
        for _ in range(attempts)
    ]
    return {
        "attempts": rows,
        "limit_cny": "30",
        "local_experiment_policy": {
            "mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
            "active_scope_id": EXPERIMENT_SCOPE_ID,
            "round_attempts_used": 2,
            "round_attempt_limit": 12,
            "scope_attempt_limit": 9,
        },
    }


def test_default_readiness_report_never_enables_dispatch() -> None:
    report = build_dry_run_report(
        _config(
            _NO_IO_PATH,
            database_url=(
                f"postgresql+asyncpg://agenthub:m4@127.0.0.1:15432/{EXPERIMENT_DATABASE_NAME}"
            ),
        ),
        ledger_state=_ledger(),
        tokenizer_verified=True,
    )

    assert report["mode"] == "DRY_RUN"
    assert report["provider_dispatch_enabled"] is False
    assert report["experiment"]["planned_provider_dispatches_max"] == PLANNED_PROVIDER_DISPATCHES
    assert report["ledger"]["experiment_attempts_used"] == 2
    assert report["ledger"]["round_attempts_used"] == 2
    assert report["execute_ready"] is True


def test_readiness_report_fails_closed_for_missing_isolation_or_tokenizer() -> None:
    report = build_dry_run_report(
        _config(_NO_IO_PATH), ledger_state=_ledger(), tokenizer_verified=False
    )

    assert report["execute_ready"] is False
    assert EXPERIMENT_DATABASE_URL_ENV in report["missing_configuration_or_budget"]
    assert "PINNED_TOKENIZER_ASSET" in report["missing_configuration_or_budget"]
    assert report["provider_dispatch_enabled"] is False


def test_database_identity_accepts_only_named_local_isolated_database() -> None:
    safe = safe_database_identity(
        f"postgresql+asyncpg://user:secret@127.0.0.1:15432/{EXPERIMENT_DATABASE_NAME}"
    )
    assert safe == {
        "host": "127.0.0.1",
        "port": 15432,
        "database": EXPERIMENT_DATABASE_NAME,
    }
    assert safe_database_identity("postgresql://u:p@127.0.0.1:5432/business") is None
    assert (
        safe_database_identity("postgresql://u:p@example.test:15432/" + EXPERIMENT_DATABASE_NAME)
        is None
    )


def test_provider_route_is_exact_beijing_https_endpoint() -> None:
    assert is_allowlisted_beijing_url(
        "https://dashscope.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    assert not is_allowlisted_beijing_url(
        "http://dashscope.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    assert not is_allowlisted_beijing_url(
        "https://dashscope.cn-beijing.maas.aliyuncs.com.evil.test/compatible-mode/v1"
    )
    assert not is_allowlisted_beijing_url("https://dashscope.cn-beijing.maas.aliyuncs.com/other")


def test_load_config_uses_only_explicit_rag_credential_names() -> None:
    config = load_config(
        env_file=Path("missing-test-env-file"),
        environ={
            RAG_API_KEY_ENV: "rag-secret",
            RAG_BASE_URL_ENV: "https://dashscope.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "OPENAI_API_KEY": "wrong-secret",
            "ALIYUN_API_KEY": "wrong-secret",
            "AGENTHUB_M4_ALIYUN_API_KEY": "wrong-secret",
        },
    )
    assert config.api_key == "rag-secret"
    assert config.base_url == "https://dashscope.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


@pytest.mark.parametrize(
    ("execute", "accept", "error"),
    [
        (True, False, "PROVIDER_DISPATCH_ACCEPTANCE_REQUIRED"),
        (False, True, "ACCEPTANCE_FLAG_REQUIRES_EXECUTE"),
    ],
)
def test_provider_dispatch_requires_both_explicit_flags(
    execute: bool, accept: bool, error: str
) -> None:
    with pytest.raises(ThreadRunExperimentError, match=error):
        require_dispatch_acceptance(execute=execute, accept_provider_dispatch=accept)


def test_fixture_preserves_original_user_text_in_three_budgeted_turns() -> None:
    assert len(THREAD_FIXTURE) == 3
    assert any("更正" in turn.user_input and "60 天" in turn.user_input for turn in THREAD_FIXTURE)
    assert "45 天" in FIRST_RUN_INPUT and "60 天" in FIRST_RUN_INPUT
    assert "撤销通知" in NEXT_RUN_INPUT
    assert len(LONG_FIXTURE_APPENDICES) == 6
    for original in _ORIGINAL_FIXTURE_USER_TEXT:
        assert sum(original in turn.user_input for turn in THREAD_FIXTURE) == 1
    assert "人工构造" not in THREAD_FIXTURE[0].user_input
    assert "虚构业务背景补充" in ELIGIBLE_ASSISTANT_BACKGROUND
    assert "ACTIVE只决定是否进入常规候选扫描，是必要条件而不是充分授权。" in (
        ELIGIBLE_ASSISTANT_BACKGROUND
    )


def test_summary_preflight_requires_trigger_prefix_protected_tail_and_budget_fit() -> None:
    assert WORKING_INPUT_LIMIT == 6000
    metrics = {
        "summary_policy_enabled": True,
        "summary_trigger_expected": True,
        "summary_source_policy": "plain_text_prefix_v1",
        "checkpoint_selector_exact": True,
        "approval_service_configured": True,
        "run_budget_account_ready": True,
        "run_execution_lease_clear": True,
        "summary_request_mode": "non_thinking",
        "summary_request_exact_tokens": 1700,
        "summary_request_approx_tokens": 1800,
        "summary_request_context_admission_code": None,
        "summary_request_available_input_tokens": 2200,
        "summary_request_within_working_budget": True,
        "pre_trim_pressure": "SOFT",
        "prefix_candidate_count": 6,
        "recent_turn_count": 2,
        "recent_groups_retained": True,
        "projected_final_context_tokens": 2132,
        "B_work": WORKING_INPUT_LIMIT,
    }
    _assert_summary_preflight_metrics(metrics)
    metrics["projected_final_context_tokens"] = WORKING_INPUT_LIMIT + 1
    with pytest.raises(
        ThreadRunExperimentError,
        match="SUMMARY_PREFLIGHT_FINAL_CONTEXT_OVER_BUDGET",
    ):
        _assert_summary_preflight_metrics(metrics)


def test_summary_preflight_rejects_missing_policy_or_runtime_trigger() -> None:
    metrics = {
        "summary_policy_enabled": False,
        "summary_trigger_expected": False,
        "summary_source_policy": "plain_text_prefix_v1",
        "checkpoint_selector_exact": True,
        "approval_service_configured": True,
        "run_budget_account_ready": True,
        "run_execution_lease_clear": True,
        "summary_request_mode": "non_thinking",
        "summary_request_exact_tokens": 1200,
        "summary_request_approx_tokens": 1400,
        "summary_request_context_admission_code": None,
        "summary_request_available_input_tokens": 2200,
        "summary_request_within_working_budget": True,
        "pre_trim_pressure": "SOFT",
        "prefix_candidate_count": 6,
        "recent_turn_count": 2,
        "recent_groups_retained": True,
        "projected_final_context_tokens": 2000,
        "B_work": WORKING_INPUT_LIMIT,
    }
    with pytest.raises(ThreadRunExperimentError, match="SUMMARY_PREFLIGHT_POLICY_NOT_ENABLED"):
        _assert_summary_preflight_metrics(metrics)
    metrics["summary_policy_enabled"] = True
    with pytest.raises(ThreadRunExperimentError, match="SUMMARY_PREFLIGHT_TRIGGER_NOT_REACHED"):
        _assert_summary_preflight_metrics(metrics)


def test_first_main_request_requires_published_summary_envelope() -> None:
    request = ModelRequest(
        messages=(
            ModelMessage(
                "assistant",
                '{"untrusted_summary":{"trust":"UNTRUSTED_DERIVED_SUMMARY",'
                '"content":{"goal":{"text":"historical only"}}}}',
            ),
            ModelMessage("user", "current task"),
        ),
        required_capabilities=CapabilityRequirements(required=frozenset({"tool_calling"})),
    )
    assert _request_has_summary_view(request)
    untrimmed = ModelRequest(
        messages=(ModelMessage("user", "full old history and current task"),),
        required_capabilities=CapabilityRequirements(required=frozenset({"tool_calling"})),
    )
    assert not _request_has_summary_view(untrimmed)


def test_run_checkpoint_config_is_bound_to_exact_tenant_and_run() -> None:
    workspace_id, run_id = uuid4(), uuid4()
    config = LangGraphCheckpointAdapter.config_for_run(workspace_id, run_id)
    _assert_run_checkpoint_selector(config, workspace_id=workspace_id, run_id=run_id)
    config["configurable"]["thread_id"] = f"agenthub:{workspace_id}:{uuid4()}"
    with pytest.raises(
        ThreadRunExperimentError,
        match="SUMMARY_CHECKPOINT_SELECTOR_BINDING_INVALID",
    ):
        _assert_run_checkpoint_selector(config, workspace_id=workspace_id, run_id=run_id)


def test_three_call_budget_reserves_summary_first_answer_and_next_run() -> None:
    assert PLANNED_PROVIDER_DISPATCHES == 3
    report = build_dry_run_report(
        _config(
            _NO_IO_PATH,
            database_url=(
                f"postgresql+asyncpg://agenthub:m4@127.0.0.1:15432/{EXPERIMENT_DATABASE_NAME}"
            ),
        ),
        ledger_state=_ledger(attempts=2),
        tokenizer_verified=True,
    )
    assert report["experiment"]["planned_provider_dispatches_max"] == 3
    assert report["execute_ready"] is True


@pytest.mark.asyncio
async def test_recording_gateway_streams_and_records_response_without_replacing_gateway() -> None:
    response = ModelResponse(
        content="diagnostic response",
        provider="openai-compatible",
        model="deepseek-v4.1-flash-1017",
        usage=ModelUsage(input_tokens=12, output_tokens=3, total_tokens=15),
    )

    class Gateway:
        async def stream_resolved(self, context, plan, request):
            del context, plan, request
            yield ModelStreamEvent(ModelStreamEventType.MESSAGE_DELTA, message_delta="diagnostic")
            yield ModelStreamEvent(ModelStreamEventType.COMPLETED, response=response)

    recorder = _DispatchRecorder()
    profile = SimpleNamespace(provider="openai-compatible", model="deepseek-v4.1-flash-1017")
    plan = SimpleNamespace(primary=profile)
    request = ModelRequest(
        messages=(ModelMessage("system", "safe"), ModelMessage("user", "diagnostic")),
        required_capabilities=CapabilityRequirements(required=frozenset({"tool_calling"})),
    )
    gateway = _RecordingPreparedGateway(Gateway(), recorder, uuid4())

    async def collect():
        return [event async for event in gateway.stream_resolved(None, plan, request)]

    events = await collect()
    assert [event.event_type for event in events] == [
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.COMPLETED,
    ]
    record = recorder.safe_records()[0]
    assert record["response"] == response.content
    assert record["usage"] == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "cached_tokens": None,
    }


@pytest.mark.asyncio
async def test_first_run_missing_summary_stops_before_underlying_gateway() -> None:
    class Gateway:
        called = False

        async def stream_resolved(self, context, plan, request):
            del context, plan, request
            self.called = True
            raise AssertionError("underlying provider boundary must stay closed")
            yield  # pragma: no cover

    underlying = Gateway()
    profile = SimpleNamespace(provider="openai-compatible", model="deepseek-v4.1-flash-1017")
    plan = SimpleNamespace(primary=profile)
    request = ModelRequest(
        messages=(ModelMessage("user", "untrimmed history and current task"),),
        required_capabilities=CapabilityRequirements(required=frozenset({"tool_calling"})),
    )
    gateway = _RecordingPreparedGateway(
        underlying,
        _DispatchRecorder(),
        uuid4(),
        require_summary_view=True,
    )

    with pytest.raises(
        ThreadRunExperimentError,
        match="SUMMARY_REVISION_MISSING_BEFORE_MAIN_MODEL",
    ):
        async for _ in gateway.stream_resolved(None, plan, request):
            pass
    assert underlying.called is False


@pytest.mark.asyncio
async def test_sync_main_model_missing_summary_is_zero_gateway_dispatches() -> None:
    class Gateway:
        called = False

        async def generate_resolved(self, context, plan, request):
            del context, plan, request
            self.called = True
            raise AssertionError("underlying provider boundary must stay closed")

    underlying = Gateway()
    profile = SimpleNamespace(provider="openai-compatible", model="deepseek-v4.1-flash-1017")
    plan = SimpleNamespace(primary=profile)
    request = ModelRequest(
        messages=(ModelMessage("user", "untrimmed history and current task"),),
        required_capabilities=CapabilityRequirements(required=frozenset({"tool_calling"})),
    )
    recorder = _DispatchRecorder()
    gateway = _RecordingPreparedGateway(
        underlying,
        recorder,
        uuid4(),
        require_summary_view=True,
    )

    with pytest.raises(
        ThreadRunExperimentError,
        match="SUMMARY_REVISION_MISSING_BEFORE_MAIN_MODEL",
    ):
        await gateway.generate_resolved(None, plan, request)
    assert underlying.called is False
    assert recorder.safe_records() == []


@pytest.mark.asyncio
async def test_sync_non_thinking_summary_request_is_not_blocked_by_task_guard() -> None:
    response = ModelResponse(
        content="{}",
        provider="openai-compatible",
        model="deepseek-v4.1-flash-1017",
    )

    class Gateway:
        called = False

        async def generate_resolved(self, context, plan, request):
            del context, plan, request
            self.called = True
            return response

    underlying = Gateway()
    profile = SimpleNamespace(provider="openai-compatible", model="deepseek-v4.1-flash-1017")
    plan = SimpleNamespace(primary=profile)
    request = ModelRequest(
        messages=(ModelMessage("system", "summary system"), ModelMessage("user", "summary data")),
        request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
    )
    recorder = _DispatchRecorder()
    gateway = _RecordingPreparedGateway(
        underlying,
        recorder,
        uuid4(),
        require_summary_view=True,
    )

    assert await gateway.generate_resolved(None, plan, request) == response
    assert underlying.called is True
    assert len(recorder.safe_records()) == 1


def test_round_budget_preflight_keeps_prior_dispatches_in_count() -> None:
    report = build_dry_run_report(
        _config(
            _NO_IO_PATH,
            database_url=(
                f"postgresql+asyncpg://agenthub:m4@127.0.0.1:15432/{EXPERIMENT_DATABASE_NAME}"
            ),
        ),
        ledger_state=_ledger(attempts=10),
        tokenizer_verified=True,
    )
    assert report["ledger"]["experiment_attempts_used"] == 10
    assert report["execute_ready"] is False
    assert "EXPERIMENT_DISPATCH_LIMIT" in report["missing_configuration_or_budget"]


def test_cost_preflight_uses_conservative_full_window_reservation() -> None:
    report = build_dry_run_report(
        _config(_NO_IO_PATH), ledger_state=_ledger(), tokenizer_verified=True
    )
    assert Decimal(report["ledger"]["remaining_cny"]) > Decimal("0")
    assert "GLOBAL_CNY_LIMIT" not in report["missing_configuration_or_budget"]


def test_sequential_run_permits_keep_stable_revision_scope_but_recheck_run_membership() -> None:
    class AccountingHook:
        def reserve_dispatch(self, **kwargs):
            pass

        def mark_unknown(self, reservation, *, reason):
            pass

        def settle_response(self, reservation, *, usage, provider, model):
            pass

        def report(self):
            return {}

    if not DEFAULT_TOKENIZER_PATH.is_file():
        pytest.skip("The local digest-pinned experiment tokenizer is unavailable")
    counter = DeepseekV41SummaryExperimentGate.from_local_asset(
        DEFAULT_TOKENIZER_PATH, output_token_limit=512
    ).counter
    workspace_id, thread_id, version_id = uuid4(), uuid4(), uuid4()
    first_run_id, second_run_id = uuid4(), uuid4()
    shared = {
        "experiment_id": EXPERIMENT_SCOPE_ID,
        "database_host": "127.0.0.1",
        "database_port": 15432,
        "database_name": EXPERIMENT_DATABASE_NAME,
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "agent_version_id": version_id,
        "resolved_spec_hash": "a" * 64,
        "provider": "openai-compatible",
        "model": "deepseek-v4.1-flash-1017",
        "base_url": "https://sample.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "output_token_limit": 512,
        "counter": counter,
        "accounting_hook": AccountingHook(),
    }
    first = LocalSummaryExperimentPermit(**shared, run_ids=frozenset({first_run_id}))
    next_run = LocalSummaryExperimentPermit(
        **shared, run_ids=frozenset({first_run_id, second_run_id})
    )

    assert first.run_ids == frozenset({first_run_id})
    assert second_run_id not in first.run_ids
    assert second_run_id in next_run.run_ids
    assert first.source_scope_payload() == next_run.source_scope_payload()
    assert "run_ids" not in next_run.source_scope_payload()
    request = ModelRequest(
        messages=(ModelMessage("system", "local"), ModelMessage("user", "fictional")),
        required_capabilities=CapabilityRequirements(required=frozenset({"tool_calling"})),
    )
    request_identity = first.request_hash(
        run_id=first_run_id,
        attempt_kind="MAIN_MODEL",
        provider="openai-compatible",
        model="deepseek-v4.1-flash-1017",
        request=request,
        output_tokens=512,
        temperature=Decimal("0.1"),
    )
    assert len(request_identity) == 64


def test_output_token_limit_is_1536_and_expected_dispatches_reject_truncation() -> None:
    assert OUTPUT_TOKEN_LIMIT == 1536
    evidence = {
        "runs": [{"run_id": "run-1"}, {"run_id": "run-2"}],
        "ledger": {"attempt_count": 26},
        "provider_dispatches": [
            {
                "run_id": "run-1",
                "provider": "openai-compatible",
                "model": "deepseek-v4.1-flash-1017",
                "request_mode": "non_thinking",
                "response": "{}",
                "usage": {"input_tokens": 5074, "output_tokens": 700, "total_tokens": 5774},
                "estimated_cost_cny": "0.01574800",
                "response_finish_reason": "stop",
            },
            {
                "run_id": "run-1",
                "provider": "openai-compatible",
                "model": "deepseek-v4.1-flash-1017",
                "request_mode": "provider_default",
                "response": "F1-F4 answer",
                "usage": {"input_tokens": 2367, "output_tokens": 1180, "total_tokens": 3547},
                "estimated_cost_cny": "0.01417400",
                "response_finish_reason": "stop",
            },
            {
                "run_id": "run-2",
                "provider": "openai-compatible",
                "model": "deepseek-v4.1-flash-1017",
                "request_mode": "provider_default",
                "response": "N1-N5 answer",
                "usage": {"input_tokens": 2443, "output_tokens": 604, "total_tokens": 3047},
                "estimated_cost_cny": "0.00971800",
                "response_finish_reason": "stop",
            },
        ],
    }
    _assert_expected_dispatches(evidence, expected_attempt_count=26)
    evidence["provider_dispatches"][1]["response_finish_reason"] = "length"
    with pytest.raises(ThreadRunExperimentError, match="PROVIDER_RESPONSE_TRUNCATED_OR_INCOMPLETE"):
        _assert_expected_dispatches(evidence, expected_attempt_count=26)
