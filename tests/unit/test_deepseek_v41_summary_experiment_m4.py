from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.engine import make_url

import pytest

from packages.agent_runtime.frozen import (
    SUMMARY_NON_THINKING_MODEL,
    SUMMARY_NON_THINKING_PROVIDER,
    SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
)
from packages.model_gateway.contracts import (
    AliyunDeepSeekRequestMode,
    ModelMessage,
    ModelRequest,
    RetryPolicy,
)
from benchmarks.evaluation.deepseek_v41_summary_experiment import (
    DEFAULT_TOKENIZER_PATH,
    DeepseekV41SummaryExperimentGate,
    MemoryTrialBudgetAccountingHook,
    SummaryExperimentAdmissionError,
)
from benchmarks.evaluation.memory_trial_budget import (
    MemoryTrialBudget,
    upper_bound_cost_cny,
)
from benchmarks.evaluation.memory_trial_client import PRICE_IDENTITY
from tests.unit.test_memory_trial_budget import (
    LEGACY_PRICE_IDENTITY,
    _enable_independent_scope,
    _seed_verified_legacy_ledger,
)

_BASE_URL = "https://sample.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
_ASSET = Path(DEFAULT_TOKENIZER_PATH)


@pytest.fixture(scope="module")
def gate() -> DeepseekV41SummaryExperimentGate:
    if not _ASSET.is_file():
        pytest.skip("The local digest-pinned experiment tokenizer is unavailable")
    return DeepseekV41SummaryExperimentGate.from_local_asset(
        _ASSET,
        output_token_limit=128,
    )


def _request(*, messages=None, mode=AliyunDeepSeekRequestMode.NON_THINKING):
    return ModelRequest(
        messages
        or (
            ModelMessage("system", "Return exactly the two characters OK."),
            ModelMessage("user", "This is a connectivity check. Reply as instructed."),
        ),
        retry_policy=RetryPolicy(max_attempts=1),
        request_mode=mode,
    )


def _admit(gate, request=None, **overrides):
    args = {
        "route_allowlist": SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
        "base_url": _BASE_URL,
        "provider": SUMMARY_NON_THINKING_PROVIDER,
        "model": SUMMARY_NON_THINKING_MODEL,
        "input_token_limit": 64,
        "requested_output_tokens": 32,
    }
    args.update(overrides)
    return gate.admit(request or _request(), **args)


def test_local_real_asset_admits_the_full_22_token_summary_request(gate):
    admitted = _admit(gate)

    assert admitted.input_tokens == 22
    assert admitted.input_token_limit == 64
    assert admitted.output_tokens_reserved == 32
    assert admitted.route_allowlist == SUMMARY_NON_THINKING_ROUTE_ALLOWLIST
    assert admitted.provider == SUMMARY_NON_THINKING_PROVIDER
    assert admitted.model == SUMMARY_NON_THINKING_MODEL
    assert admitted.request_mode == "non_thinking"
    assert admitted.thinking is False
    assert admitted.request_counter_sha256


def test_missing_or_unverified_asset_fails_closed():
    missing_path = Path(__file__).with_name("_missing_local_summary_tokenizer.json")
    assert not missing_path.exists()
    with pytest.raises(SummaryExperimentAdmissionError) as missing:
        DeepseekV41SummaryExperimentGate.from_local_asset(
            missing_path,
            output_token_limit=128,
        )
    assert missing.value.code == "SUMMARY_EXPERIMENT_COUNTER_UNAVAILABLE"

    with pytest.raises(SummaryExperimentAdmissionError) as unverified:
        DeepseekV41SummaryExperimentGate(
            object(),
            output_token_limit=128,
        )
    assert unverified.value.code == "SUMMARY_EXPERIMENT_COUNTER_UNVERIFIED"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"route_allowlist": "other-route"}, "SUMMARY_EXPERIMENT_ROUTE_REJECTED"),
        ({"base_url": "https://sample.cn-beijing.maas.aliyuncs.com/other"}, "SUMMARY_EXPERIMENT_ROUTE_REJECTED"),
        ({"base_url": "http://sample.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"}, "SUMMARY_EXPERIMENT_ROUTE_REJECTED"),
        ({"provider": "deepseek"}, "SUMMARY_EXPERIMENT_MODEL_REJECTED"),
        ({"model": "deepseek-v4.1-flash"}, "SUMMARY_EXPERIMENT_MODEL_REJECTED"),
        ({"input_token_limit": 21}, "SUMMARY_EXPERIMENT_INPUT_BUDGET_EXCEEDED"),
        ({"requested_output_tokens": 129}, "SUMMARY_EXPERIMENT_BUDGET_INVALID"),
    ],
)
def test_route_model_and_budget_failures_reject_before_runner_admission(gate, overrides, code):
    with pytest.raises(SummaryExperimentAdmissionError) as failure:
        _admit(gate, **overrides)
    assert failure.value.code == code


def test_provider_default_and_non_complete_messages_are_rejected(gate):
    with pytest.raises(SummaryExperimentAdmissionError) as mode_failure:
        _admit(gate, _request(mode=AliyunDeepSeekRequestMode.PROVIDER_DEFAULT))
    assert mode_failure.value.code == "SUMMARY_EXPERIMENT_REQUEST_REJECTED"

    with pytest.raises(SummaryExperimentAdmissionError) as message_failure:
        _admit(
            gate,
            _request(messages=(ModelMessage("user", "missing system message"),)),
        )
    assert message_failure.value.code == "SUMMARY_EXPERIMENT_REQUEST_REJECTED"


class _FakeExperimentLedger(MemoryTrialBudget):
    def __init__(self, scope_id):
        self.state = {
            "price_identity": PRICE_IDENTITY,
            "attempts": [],
            "local_experiment_policy": {
                "mode": "RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT",
                "active_scope_id": scope_id,
                "round_attempts_used": 2,
                "round_attempt_limit": 12,
            },
        }
        self.reserves = []
        self.settlements = []

    def reserve(self, attempt_id, **kwargs):
        self.reserves.append((attempt_id, kwargs))
        row = {
            "attempt_id": attempt_id,
            "status": "RESERVED_OR_UNKNOWN",
            "request_identity": kwargs["request_identity"],
            "input_counter": kwargs["input_counter"],
            "charged_or_reserved_cny": str(kwargs["upper_cost_cny"]),
            "stage": kwargs["stage"],
            "estimated_input_tokens": kwargs["estimated_input_tokens"],
            "requested_output_tokens": kwargs["requested_output_tokens"],
        }
        self.state["attempts"].append(row)

    def settle(self, attempt_id, **kwargs):
        self.settlements.append((attempt_id, kwargs))
        row = next(item for item in self.state["attempts"] if item["attempt_id"] == attempt_id)
        row["status"] = "SETTLED"
        row["charged_or_reserved_cny"] = str(kwargs["conservative_cost_cny"])
        row["result_identity"] = kwargs["result_identity"]


def test_accounting_hook_reserves_exact_counter_identity_and_preserves_unknown():
    scope_id = "m4-summary-experiment-v1"
    ledger = _FakeExperimentLedger(scope_id)
    hook = MemoryTrialBudgetAccountingHook(ledger, scope_id=scope_id)
    identity = "a" * 64
    input_counter = {
        "counter_id": "deepseek_v41_text_v1",
        "tokenizer_revision": "9f9818127fdfbb60feba204799d617a874b32a86",
        "thinking_mode": "NON_THINKING",
        "tokens": 22,
        "input_limit": 64,
        "request_identity": identity,
    }
    cost = upper_bound_cost_cny(
        input_token_bound=32768,
        output_token_bound=128,
        input_per_million=Decimal("2"),
        output_per_million=Decimal("8"),
    )
    reservation = hook.reserve_dispatch(
        experiment_id=scope_id,
        attempt_id="m4s-summary-001",
        run_id=uuid4(),
        attempt_kind="SUMMARY",
        provider=SUMMARY_NON_THINKING_PROVIDER,
        model=SUMMARY_NON_THINKING_MODEL,
        request_identity=identity,
        estimated_input_tokens=32768,
        requested_output_tokens=128,
        maximum_cost_cny=cost,
        currency="CNY",
        input_counter=input_counter,
    )

    assert reservation.request_identity == identity
    assert ledger.reserves[0][1]["input_counter"] == input_counter
    hook.mark_unknown(reservation, reason="timeout")
    assert ledger.state["attempts"][0]["status"] == "RESERVED_OR_UNKNOWN"
    assert hook.report()["round_attempts_used"] == 2


def test_runtime_permit_settles_using_the_reservation_and_response_receipts(gate):
    scope_id = "m4-summary-experiment-v1"
    directory = Path(".scratch/context-policy-m4-20261028/runtime-ledger-tests") / uuid4().hex
    directory.mkdir(parents=True)
    ledger_path = directory / "trial.json"
    _seed_verified_legacy_ledger(ledger_path)
    with MemoryTrialBudget(ledger_path, price_identity=LEGACY_PRICE_IDENTITY) as ledger:
        _enable_independent_scope(ledger, scope_id=scope_id)
        hook = MemoryTrialBudgetAccountingHook(ledger, scope_id=scope_id)
        permit = gate.bind_runtime_permit(
            experiment_id=scope_id,
            database_url=(
                "postgresql+asyncpg://agenthub:local@127.0.0.1:15432/"
                "agenthub_run_budget_m4_20261028_v4"
            ),
            workspace_id=uuid4(),
            thread_id=uuid4(),
            agent_version_id=uuid4(),
            resolved_spec_hash="d" * 64,
            run_ids={uuid4()},
            base_url=_BASE_URL,
            accounting_hook=hook,
        )
        identity = "d" * 64
        attempt_id = "m4-response-settlement"
        upper_cost = upper_bound_cost_cny(
            input_token_bound=32768,
            output_token_bound=32,
            input_per_million=Decimal("2"),
            output_per_million=Decimal("8"),
        )
        reservation = permit.reserve_dispatch(
            attempt_id=attempt_id,
            run_id=uuid4(),
            attempt_kind="SUMMARY",
            provider=SUMMARY_NON_THINKING_PROVIDER,
            model=SUMMARY_NON_THINKING_MODEL,
            request_identity=identity,
            estimated_input_tokens=32768,
            requested_output_tokens=32,
            maximum_cost_cny=upper_cost,
            currency="CNY",
            input_counter={
                "counter_id": "deepseek_v41_text_v1",
                "tokenizer_revision": "9f9818127fdfbb60feba204799d617a874b32a86",
                "thinking_mode": "NON_THINKING",
                "tokens": 22,
                "input_limit": 32768,
                "request_identity": identity,
            },
        )
        response_usage = SimpleNamespace(input_tokens=22, output_tokens=4, total_tokens=26)
        for _ in range(2):
            permit.settle_response(
                reservation,
                usage=response_usage,
                provider=SUMMARY_NON_THINKING_PROVIDER,
                model=SUMMARY_NON_THINKING_MODEL,
            )

        row = next(row for row in ledger.state["attempts"] if row["attempt_id"] == attempt_id)
        assert row["status"] == "SETTLED"
        assert row["request_identity"] == identity
        assert row["result_identity"]
        assert Decimal(row["charged_or_reserved_cny"]) == Decimal("0.000076")


def test_runtime_non_thinking_usage_mismatch_keeps_full_reservation(gate):
    scope_id = "m4-summary-experiment-v1"
    directory = Path(".scratch/context-policy-m4-20261028/runtime-ledger-tests") / uuid4().hex
    directory.mkdir(parents=True)
    ledger_path = directory / "trial.json"
    _seed_verified_legacy_ledger(ledger_path)
    with MemoryTrialBudget(ledger_path, price_identity=LEGACY_PRICE_IDENTITY) as ledger:
        _enable_independent_scope(ledger, scope_id=scope_id)
        hook = MemoryTrialBudgetAccountingHook(ledger, scope_id=scope_id)
        permit = gate.bind_runtime_permit(
            experiment_id=scope_id,
            database_url=(
                "postgresql+asyncpg://agenthub:local@127.0.0.1:15432/"
                "agenthub_run_budget_m4_20261028_v4"
            ),
            workspace_id=uuid4(),
            thread_id=uuid4(),
            agent_version_id=uuid4(),
            resolved_spec_hash="e" * 64,
            run_ids={uuid4()},
            base_url=_BASE_URL,
            accounting_hook=hook,
        )
        identity = "f" * 64
        attempt_id = "m4-counter-mismatch"
        upper_cost = upper_bound_cost_cny(
            input_token_bound=32768,
            output_token_bound=32,
            input_per_million=Decimal("2"),
            output_per_million=Decimal("8"),
        )
        reservation = permit.reserve_dispatch(
            attempt_id=attempt_id,
            run_id=uuid4(),
            attempt_kind="SUMMARY",
            provider=SUMMARY_NON_THINKING_PROVIDER,
            model=SUMMARY_NON_THINKING_MODEL,
            request_identity=identity,
            estimated_input_tokens=32768,
            requested_output_tokens=32,
            maximum_cost_cny=upper_cost,
            currency="CNY",
            input_counter={
                "counter_id": "deepseek_v41_text_v1",
                "tokenizer_revision": "9f9818127fdfbb60feba204799d617a874b32a86",
                "thinking_mode": "NON_THINKING",
                "tokens": 22,
                "input_limit": 32768,
                "request_identity": identity,
            },
        )
        with pytest.raises(ValueError, match="PROVIDER_COUNTER_MISMATCH"):
            permit.settle_response(
                reservation,
                usage=SimpleNamespace(input_tokens=23, output_tokens=4, total_tokens=27),
                provider=SUMMARY_NON_THINKING_PROVIDER,
                model=SUMMARY_NON_THINKING_MODEL,
            )
        row = next(row for row in ledger.state["attempts"] if row["attempt_id"] == attempt_id)
        assert row["status"] == "RESERVED_OR_UNKNOWN"
        assert row["charged_or_reserved_cny"] == row["upper_cost_cny"]


def test_runtime_permit_binds_database_frozen_run_set_and_accounting_hook(gate):
    scope_id = "m4-summary-experiment-v1"
    hook = MemoryTrialBudgetAccountingHook(_FakeExperimentLedger(scope_id), scope_id=scope_id)
    workspace_id, thread_id, version_id, run_id = (uuid4() for _ in range(4))
    database_url = (
        "postgresql+asyncpg://agenthub:local@127.0.0.1:15432/"
        "agenthub_run_budget_m4_20261028_v4"
    )
    spec_hash = "c" * 64
    permit = gate.bind_runtime_permit(
        experiment_id=scope_id,
        database_url=database_url,
        workspace_id=workspace_id,
        thread_id=thread_id,
        agent_version_id=version_id,
        resolved_spec_hash=spec_hash,
        run_ids={run_id},
        base_url=_BASE_URL,
        accounting_hook=hook,
    )
    session_factory = SimpleNamespace(
        kw={"bind": SimpleNamespace(url=make_url(database_url))}
    )
    frozen = SimpleNamespace(
        runtime={"summary_policy": permit.request_identity},
        model_plan=SimpleNamespace(
            primary=SimpleNamespace(
                provider=SUMMARY_NON_THINKING_PROVIDER,
                model=SUMMARY_NON_THINKING_MODEL,
            ),
            fallbacks=(),
        ),
    )

    assert permit.matches_session_factory(session_factory)
    assert permit.authorizes_run(
        session_factory=session_factory,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        agent_version_id=version_id,
        resolved_spec_hash=spec_hash,
        spec=frozen,
    )
    assert not permit.authorizes_run(
        session_factory=session_factory,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=uuid4(),
        agent_version_id=version_id,
        resolved_spec_hash=spec_hash,
        spec=frozen,
    )
    wrong_database = SimpleNamespace(
        kw={"bind": SimpleNamespace(url=make_url(database_url.replace("_v4", "_v5")))}
    )
    assert not permit.matches_session_factory(wrong_database)
