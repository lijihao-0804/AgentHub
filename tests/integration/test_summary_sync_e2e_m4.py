"""M4 summary path through real threaded LangGraph runs.

These tests use only the dedicated M4 v4 PostgreSQL database and offline
provider adapters. No external model API is called.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import packages.agent_runtime.runtime_graph as runtime_graph_module
import packages.model_gateway.gateway as model_gateway_module
from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.context_policy import resolve_context_policy
from packages.agent_runtime.model_budget_adapter import RunBudgetAttemptAdapter
from packages.agent_runtime.models import AgentRun, AgentVersion, RunStep
from packages.agent_runtime.run_budget_ledger import (
    RunBudgetAttempt,
    RunBudgetAttemptKind,
    RunBudgetLedger,
)
from packages.agent_runtime.runtime import AgentRunService
from packages.agent_runtime.runtime_config import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_MEMORY_CONFIG,
    DEFAULT_RUNTIME_LIMITS,
)
from packages.agent_runtime.summary_budget_adapter import SummaryRunBudgetReserver
from packages.agent_runtime.summary_models import ThreadSummaryArtifact, ThreadSummaryJob
from packages.agent_runtime.summary_prompt import SUMMARY_PROMPT_V1
from packages.agent_runtime.summary_worker import SummaryWorker
from packages.agent_runtime.work_layer import PLAIN_TEXT_PREFIX_V1
from packages.approvals.contracts import ApprovalDecisionStatus
from packages.approvals.service import ApprovalService
from packages.control_plane.services import TenantService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.evaluation.models import PricingSnapshot
from packages.evaluation.reproducibility import pricing_snapshot_content_hash
from packages.model_gateway.contracts import ModelRequest, ModelResponse, ModelToolCall, ModelUsage
from packages.model_gateway.credentials import (
    ProviderCredentialCipher,
)
from packages.model_gateway.gateway import (
    ModelGatewayService,
    SqlAlchemyModelGateway,
    _PreparedModelGateway,
)
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.model_gateway.run_budget_adapter import AttemptCostUpperBound
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.service import ThreadService
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer
from tests.integration.test_m4c_agent_runtime import _seed

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
EXPECTED_DATABASE_URL = (
    "postgresql+psycopg://agenthub:m2_local_test@127.0.0.1:15432/agenthub_run_budget_m4_20261028_v4"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to the isolated M4 v4 PostgreSQL database.",
    ),
]


@pytest.fixture(scope="session")
def migrated_v4_database():
    """Migrate only the explicitly named disposable v4 integration database."""
    normalized = TEST_DATABASE_URL.lower()
    if not (
        TEST_DATABASE_URL == EXPECTED_DATABASE_URL
        or "test" in normalized
        or "budget" in normalized
    ):
        pytest.fail("This test may use only agenthub_run_budget_m4_20261028_v4.")
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


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


@pytest_asyncio.fixture
async def db_factory(
    migrated_v4_database,
) -> async_sessionmaker[AsyncSession]:
    del migrated_v4_database
    engine, factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


class _ControlledFakeGateway:
    """Record actual runtime ModelRequests and return local deterministic text."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def generate_resolved(self, context, plan, request: ModelRequest) -> ModelResponse:
        del context
        self.requests.append(request)
        return ModelResponse(
            content=f"CONTROLLED ANSWER {len(self.requests)}",
            provider=plan.primary.provider,
            model=plan.primary.model,
        )


class _FixedAttemptPrice:
    async def resolve(self, session, *, profile, request, provider, attempt_kind):
        del session, profile, request, provider, attempt_kind
        return AttemptCostUpperBound(
            amount=Decimal("0.01"),
            currency="CNY",
            basis_identity="isolated-fake-provider-flat-upper-bound-v1",
        )


def _install_dispatch_trace(monkeypatch, expected_provider):
    """Observe the actual test gateway and budget outcomes without replacing them."""
    trace = {"gateway": [], "run_budget": [], "summary_budget": []}

    original_generate = _PreparedModelGateway.generate_resolved

    async def traced_generate(self, context, plan, request):
        kind = "summary" if request.messages[0].content == SUMMARY_PROMPT_V1 else "main"
        trace["gateway"].append(
            (
                kind,
                "dispatch",
                type(self._service.adapter).__name__,
                self._service.adapter is expected_provider,
                type(self._service.attempt_budget).__name__
                if self._service.attempt_budget is not None
                else None,
            )
        )
        try:
            response = await original_generate(self, context, plan, request)
        except Exception as error:
            trace["gateway"].append((kind, "error", type(error).__name__, str(error)))
            raise
        trace["gateway"].append(
            (
                kind,
                "response",
                type(response).__name__,
                response.usage,
                response.finish_reason,
            )
        )
        return response

    original_run_reserve = RunBudgetAttemptAdapter.reserve_attempt

    async def traced_run_reserve(self, *args, **kwargs):
        try:
            reservation = await original_run_reserve(self, *args, **kwargs)
        except Exception as error:
            trace["run_budget"].append(("error", type(error).__name__, str(error)))
            raise
        trace["run_budget"].append(
            (
                "reserved",
                reservation.attempt_identity,
                reservation.cost_upper_bound.amount,
                reservation.cost_upper_bound.currency,
            )
        )
        return reservation

    original_summary_reserve = SummaryRunBudgetReserver.reserve

    async def traced_summary_reserve(self, session, **kwargs):
        try:
            created = await original_summary_reserve(self, session, **kwargs)
        except Exception as error:
            trace["summary_budget"].append(("error", type(error).__name__, str(error)))
            raise
        trace["summary_budget"].append(
            (
                "reserved" if created else "rejected",
                str(kwargs.get("amount")),
                kwargs.get("currency"),
            )
        )
        return created

    original_summary_worker = SummaryWorker.run_claimed

    async def traced_summary_worker(self, **kwargs):
        try:
            result = await original_summary_worker(self, **kwargs)
        except Exception as error:
            trace.setdefault("summary_worker", []).append(
                ("error", type(error).__name__, getattr(error, "code", None), str(error))
            )
            raise
        trace.setdefault("summary_worker", []).append(
            ("settlement", result.status, result.validation_result)
        )
        return result

    original_reserve_attempt = ModelGatewayService._reserve_attempt

    async def traced_reserve_attempt(self, *args, **kwargs):
        try:
            result = await original_reserve_attempt(self, *args, **kwargs)
        except Exception as error:
            trace["gateway_budget"] = trace.get("gateway_budget", [])
            trace["gateway_budget"].append(("error", type(error).__name__, str(error)))
            raise
        trace.setdefault("gateway_budget", []).append(
            (
                "reserved" if result is not None else "no adapter",
                type(self.attempt_budget).__name__ if self.attempt_budget is not None else None,
                result.attempt_identity if result is not None else None,
                result.cost_upper_bound.amount if result is not None else None,
                result.cost_upper_bound.currency if result is not None else None,
            )
        )
        return result

    original_validate = model_gateway_module._validate_usable_response

    def traced_validate(response):
        trace.setdefault("response_validation", []).append(
            (
                type(response).__name__,
                repr(getattr(response, "content", None))[:160],
                getattr(response, "provider", None),
                getattr(response, "model", None),
                getattr(response, "usage", None),
            )
        )
        try:
            return original_validate(response)
        except Exception as error:
            trace["response_validation"].append(("error", type(error).__name__, str(error)))
            raise

    original_candidates = runtime_graph_module._summary_candidates

    def traced_candidates(history, prefix):
        result = original_candidates(history, prefix)
        trace.setdefault("summary_candidates", []).append(
            {
                "prefix_sources": [
                    (item.source_id, item.sequence) for item in getattr(prefix, "sources", ())
                ],
                "prefix_stop_reason": getattr(prefix, "stop_reason", None),
                "turn_proofs": [
                    (
                        str(turn.turn_id),
                        getattr(getattr(turn, "plain_text_prefix_v1_proof", None), "status", None),
                        getattr(getattr(turn, "plain_text_prefix_v1_proof", None), "reason", None),
                    )
                    for turn in history.turns
                ],
                "candidate_sources": [(item.source_id, item.sequence) for item in result],
            }
        )
        return result

    original_summary_create = runtime_graph_module._AgentRunGraph._create_or_recover_summary

    async def traced_summary_create(self, **kwargs):
        config = kwargs.get("config")
        configurable = config.get("configurable") if isinstance(config, Mapping) else None
        state = kwargs.get("state")
        next_round = int(state.get("model_round_count", 0)) + 1 if isinstance(state, Mapping) else 0
        attempt_budget = (
            self.service.model_attempt_budget_factory(self.run, next_round)
            if self.service.model_attempt_budget_factory is not None
            else None
        )
        adapter = self.service.checkpoint_adapter
        expected_config = (
            adapter.config_for_run(self.run.workspace_id, self.run.id)
            if adapter is not None
            else None
        )
        expected_configurable = (
            expected_config.get("configurable") if isinstance(expected_config, Mapping) else None
        )
        entry = {
            "config_type": type(config).__name__ if config is not None else None,
            "config_keys": tuple(config.keys()) if isinstance(config, Mapping) else (),
            "checkpoint_id": configurable.get("checkpoint_id")
            if isinstance(configurable, Mapping)
            else None,
            "checkpoint_ns": configurable.get("checkpoint_ns")
            if isinstance(configurable, Mapping)
            else None,
            "checkpoint_map": dict(configurable.get("checkpoint_map", {}))
            if isinstance(configurable, Mapping)
            and isinstance(configurable.get("checkpoint_map"), Mapping)
            else None,
            "expected_checkpoint_ns": expected_configurable.get("checkpoint_ns")
            if isinstance(expected_configurable, Mapping)
            else None,
            "expected_thread_id": expected_configurable.get("thread_id")
            if isinstance(expected_configurable, Mapping)
            else None,
            "actual_thread_id": configurable.get("thread_id")
            if isinstance(configurable, Mapping)
            else None,
            "has_budget_factory": self.service.model_attempt_budget_factory is not None,
            "attempt_budget_bound": (
                attempt_budget.is_bound_to_run(
                    UUID(self.context.workspace_id), self.run.id, f"round-{next_round}"
                )
                if attempt_budget is not None
                else None
            ),
            "has_checkpoint_adapter": self.service.checkpoint_adapter is not None,
        }
        try:
            result = await original_summary_create(self, **kwargs)
        except Exception as error:
            entry["error"] = (type(error).__name__, getattr(error, "code", None), str(error))
            trace.setdefault("summary_create", []).append(entry)
            raise
        entry["result"] = (
            (type(result).__name__, getattr(result, "revision_hash", None))
            if result is not None
            else None
        )
        trace.setdefault("summary_create", []).append(entry)
        return result

    monkeypatch.setattr(_PreparedModelGateway, "generate_resolved", traced_generate)
    monkeypatch.setattr(RunBudgetAttemptAdapter, "reserve_attempt", traced_run_reserve)
    monkeypatch.setattr(SummaryRunBudgetReserver, "reserve", traced_summary_reserve)
    monkeypatch.setattr(SummaryWorker, "run_claimed", traced_summary_worker)
    monkeypatch.setattr(ModelGatewayService, "_reserve_attempt", traced_reserve_attempt)
    monkeypatch.setattr(model_gateway_module, "_validate_usable_response", traced_validate)
    monkeypatch.setattr(runtime_graph_module, "_summary_candidates", traced_candidates)
    monkeypatch.setattr(
        runtime_graph_module._AgentRunGraph,
        "_create_or_recover_summary",
        traced_summary_create,
    )
    return trace


class _SummaryAwareFakeProvider:
    """Record provider-bound requests and return one schema-valid local result."""

    def __init__(self, *, approval_on_summary: bool = False) -> None:
        self.main_requests: list[ModelRequest] = []
        self.summary_requests: list[ModelRequest] = []
        self.complete_requests: list[ModelRequest] = []
        self.long_answer = "ARCHIVE-DETAIL " + ("archival context remains in its source. " * 300)
        self.approval_on_summary = approval_on_summary
        self.approval_triggered = False

    async def complete(self, profile, credential, request: ModelRequest) -> ModelResponse:
        self.complete_requests.append(request)
        provider = credential.provider
        if request.messages[0].content == SUMMARY_PROMPT_V1:
            self.summary_requests.append(request)
            payload_text = request.messages[1].content or ""
            payload = json.loads(payload_text.partition("\n")[2])
            source_ids = payload["complete_prefix"]["source_ids"]
            protected_facts = payload["protected_facts"]
            candidate = {
                "status": "OK",
                "goal": {
                    "text": "Continue the user task using the cited history.",
                    "source_ids": source_ids[:1],
                },
                "decisions": [],
                "supported_facts": [
                    {"text": fact["text"], "source_ids": fact["source_ids"]}
                    for fact in protected_facts
                ]
                + [
                    {
                        "text": "Older assistant detail is represented by its retained source.",
                        "source_ids": source_ids,
                    }
                ],
                "conflicts": [],
                "open_questions": [],
                "completed_actions_refs": [],
                "next_steps": [
                    {
                        "text": "Continue with the exact recent exchanges.",
                        "source_ids": source_ids,
                    }
                ],
                "omitted_source_ids": [],
            }
            return ModelResponse(
                content=json.dumps(candidate, ensure_ascii=False),
                provider=provider,
                model=profile.model,
                usage=ModelUsage(input_tokens=1200, output_tokens=120, total_tokens=1320),
                finish_reason="stop",
            )

        self.main_requests.append(request)
        if (
            self.approval_on_summary
            and not self.approval_triggered
            and any(
                "UNTRUSTED_DERIVED_SUMMARY" in (message.content or "")
                for message in request.messages
            )
        ):
            self.approval_triggered = True
            return ModelResponse(
                content="",
                provider=provider,
                model=profile.model,
                tool_calls=(
                    ModelToolCall(
                        name="create_ticket",
                        arguments={
                            "customer_ref": "summary-e2e-customer",
                            "subject": "Resume with the same fixed summary",
                        },
                        provider_tool_call_id="fake-summary-approval-call",
                    ),
                ),
                usage=ModelUsage(input_tokens=120, output_tokens=20, total_tokens=140),
                finish_reason="tool_calls",
            )
        user_input = request.messages[-1].content or ""
        if user_input.startswith("ARCHIVE-QUOTE-01:"):
            content = self.long_answer
            output_tokens = 1800
        elif user_input.startswith("RECENT-TURN-03:"):
            content = f"ANSWER::{user_input} RECENT-DETAIL " + (
                "The latest completed detail remains available. " * 100
            )
            output_tokens = max(1, len(content) // 4)
        else:
            content = f"ANSWER::{user_input}"
            output_tokens = max(1, len(content) // 4)
        input_tokens = 120
        return ModelResponse(
            content=content,
            provider=provider,
            model=profile.model,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            finish_reason="stop",
        )


def _summary_message(request: ModelRequest) -> str:
    return next(
        message.content
        for message in request.messages
        if "UNTRUSTED_DERIVED_SUMMARY" in (message.content or "")
    )


def _summary_content(request: ModelRequest) -> object:
    return json.loads(_summary_message(request))["untrusted_summary"]["content"]


async def _enable_summary_opt_in(
    db_factory, version_id: UUID, *, workspace_input_limit: int = 3800
):
    async with db_factory() as session:
        version = await session.scalar(select(AgentVersion).where(AgentVersion.id == version_id))
        assert version is not None
        resolved = dict(version.resolved_spec)
        model = dict(resolved["model"])
        model["max_tokens"] = 2048
        model["capabilities"] = {
            **dict(model["capabilities"]),
            "max_context_tokens": 8192,
        }
        profile = await session.get(ModelProfile, UUID(model["profile_id"]))
        assert profile is not None
        profile.max_tokens = 2048
        profile.capabilities = {"tool_calling": True, "max_context_tokens": 8192}

        credential_id = UUID(model["credential_ref"])
        credential = await session.get(ProviderCredential, credential_id)
        assert credential is not None and credential.secret_ciphertext is not None

        context_budget = {**DEFAULT_CONTEXT_BUDGET, "reserved_output_tokens": 2048}
        context_budget_sources = {
            key: ("CUSTOM" if key == "reserved_output_tokens" else "DEFAULT")
            for key in DEFAULT_CONTEXT_BUDGET
        }
        context_policy = resolve_context_policy(
            {
                "policy_version": "adaptive_context_v1",
                "preset": "GENERAL_64K",
                "workspace_input_limit": workspace_input_limit,
                "workspace_input_limit_source": "CUSTOM",
            },
            [model],
            reserved_output_tokens=2048,
        )
        runtime = {
            **DEFAULT_RUNTIME_LIMITS,
            "context_budget": context_budget,
            "context_budget_sources": context_budget_sources,
            "context_policy": context_policy,
            "memory": dict(DEFAULT_MEMORY_CONFIG),
            "history_policy": "deterministic_history_v1",
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
        }
        resolved.update(
            {
                "spec_schema_version": 5,
                "model": model,
                "retrieval": {"knowledge_bindings": []},
                "runtime": runtime,
            }
        )
        version.spec_schema_version = 5
        version.resolved_spec = resolved
        version.resolved_spec_hash = canonical_json_hash(resolved)
        await session.commit()


async def _seed_pricing_snapshot(db_factory, base: dict[str, object]) -> None:
    async with db_factory() as session:
        snapshot = PricingSnapshot(
            workspace_id=base["workspace_id"],
            name=f"summary-e2e-{uuid4().hex}",
            provider="fake",
            model="frozen-model-a",
            currency="CNY",
            input_price_per_1m=Decimal("1.00000000"),
            output_price_per_1m=Decimal("1.00000000"),
            effective_at=datetime.now(UTC) - timedelta(minutes=1),
            source_note="isolated offline integration fixture",
            content_hash="0" * 64,
            created_by=base["user"].id,
        )
        snapshot.content_hash = pricing_snapshot_content_hash(snapshot)
        session.add(snapshot)
        await session.commit()


@pytest.mark.asyncio
async def test_real_thread_runs_prove_old_prefix_and_keep_verbatim_recent_turns(db_factory):
    """Build source evidence from completed Runs; never manufacture planner groups."""
    async with db_factory() as session:
        base = await _seed(session, label=f"summary-sync-e2e-{uuid4().hex}")

    gateway = _ControlledFakeGateway()
    source_provider = SqlAlchemyThreadContextProvider(db_factory)
    run_service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda _session: gateway,
        thread_context_provider=source_provider,
    )
    thread_service = ThreadService(db_factory, run_service=run_service)
    context = base["context"]
    thread = await thread_service.create_thread(
        context,
        agent_id=base["version"].agent_id,
        title="M4 summary sync e2e",
    )

    old_verbatim = "ARCHIVE-QUOTE-01: retain exactly the phrase 'north bridge, 07:14'."
    turns = (
        old_verbatim,
        "OLDER-PREFIX-02: this completed answer is part of the eligible prefix.",
        "RECENT-TURN-03: keep this exact user utterance.",
        "RECENT-TURN-04: keep this exact user utterance too.",
    )
    submitted = []
    for sequence, user_input in enumerate(turns, start=1):
        submitted.append(
            await thread_service.submit_turn(
                context,
                thread.id,
                user_input=user_input,
                client_token=f"summary-sync-e2e-{sequence}-{uuid4().hex[:12]}",
            )
        )

    next_input = "NEXT-RUN-05: answer using the prior thread context."
    next_run = await thread_service.submit_turn(
        context,
        thread.id,
        user_input=next_input,
        client_token=f"summary-sync-e2e-next-{uuid4().hex[:12]}",
    )
    assert len(gateway.requests) == 5
    actual_request = gateway.requests[-1]
    actual_request_text = "\n".join(message.content or "" for message in actual_request.messages)
    for expected_text in (
        old_verbatim,
        "RECENT-TURN-03: keep this exact user utterance.",
        "RECENT-TURN-04: keep this exact user utterance too.",
        next_input,
    ):
        assert expected_text in actual_request_text

    history = await source_provider.history_sources(
        workspace_id=UUID(context.workspace_id),
        thread_id=thread.id,
        before_run_id=next_run.run_id,
        max_turns=10,
        max_turns_scanned=20,
        max_artifacts=20,
        artifact_page_size=2,
        summary_source_policy=PLAIN_TEXT_PREFIX_V1,
    )
    assert history.source_manifest is not None
    sources = history.source_manifest.selected_sources
    assert tuple(source.sequence for source in sources) == (1, 2, 3, 4)

    async with db_factory() as session:
        version = await session.scalar(
            select(AgentVersion).where(AgentVersion.id == base["version"].id)
        )
        persisted_runs = tuple(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.id.in_([item.run_id for item in (*submitted, next_run)])
                )
            )
        )
        assert version is not None
        assert len(persisted_runs) == 5
        assert all(run.status == "SUCCEEDED" for run in persisted_runs)

    assert len(history.turns) == len(sources) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_resume", [False, True], ids=["next-run", "resume"])
async def test_summary_fixed_revision_next_run_and_checkpoint_resume(
    db_factory, monkeypatch, checkpoint_resume
):
    """Exercise source proof, same-Run dispatch and cross-Run fixed revision use."""
    tool_spec = (
        {
            "kind": "builtin",
            "identity": "create_ticket",
            "description": "Controlled summary resume approval",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_ref": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": ["customer_ref", "subject"],
                "additionalProperties": False,
            },
            "effect": "WRITE",
            "risk_level": "HIGH",
            "approval_policy": "ALWAYS",
            "timeout_seconds": 30,
        }
        if checkpoint_resume
        else None
    )
    async with db_factory() as session:
        base = await _seed(
            session,
            label=f"summary-graph-e2e-{uuid4().hex}",
            tool_spec=tool_spec,
        )
        if checkpoint_resume:
            session.add(
                Customer(
                    workspace_id=base["workspace_id"],
                    customer_ref="summary-e2e-customer",
                    name="Summary E2E Customer",
                )
            )
            await session.commit()

    cipher = ProviderCredentialCipher.from_settings()
    await _enable_summary_opt_in(
        db_factory,
        base["version"].id,
        workspace_input_limit=4900,
    )
    await _seed_pricing_snapshot(db_factory, base)

    provider = _SummaryAwareFakeProvider(approval_on_summary=checkpoint_resume)
    dispatch_trace = _install_dispatch_trace(monkeypatch, provider)
    source_provider = SqlAlchemyThreadContextProvider(db_factory)
    ledger = RunBudgetLedger()
    price_source = _FixedAttemptPrice()

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

    approval_service = ApprovalService(db_factory)
    run_service = AgentRunService(
        db_factory,
        model_gateway_factory=model_gateway_factory,
        model_attempt_budget_factory=attempt_budget_factory,
        approval_service=approval_service,
        action_runtime=ActionRuntime(session_factory=db_factory) if checkpoint_resume else None,
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
        title="M4 summary graph e2e",
    )

    protected_user_text = 'ARCHIVE-QUOTE-01: retain exactly "north bridge, 07:14".'
    turn_inputs = (
        protected_user_text,
        "OLDER-PREFIX-02: this completed exchange precedes the recent tail.",
        "RECENT-TURN-03: keep this exact user utterance.",
        "RECENT-TURN-04: keep this exact user utterance too.",
    )
    submitted = []
    for sequence, user_input in enumerate(turn_inputs, start=1):
        submitted.append(
            await thread_service.submit_turn(
                context,
                thread.id,
                user_input=user_input,
                client_token=f"summary-graph-{sequence}-{uuid4().hex[:12]}",
            )
        )

    resume_result = None
    checkpoint_summary_binding = None
    if checkpoint_resume:
        trigger_run_id = submitted[3].run_id
        async with db_factory() as session:
            waiting = await session.scalar(select(AgentRun).where(AgentRun.id == trigger_run_id))
            waiting_steps = tuple(
                await session.scalars(
                    select(RunStep).where(
                        RunStep.agent_run_id.in_([item.run_id for item in submitted])
                    )
                )
            )
            step_summary = [(step.kind, step.status, step.safe_metadata) for step in waiting_steps]
            assert waiting is not None and waiting.status == "WAITING_APPROVAL", (
                f"expected summary-backed approval interrupt; run="
                f"{(waiting.status, waiting.failure_code) if waiting is not None else None}; "
                f"steps={step_summary}; "
                f"dispatch_trace={dispatch_trace}"
            )
        approvals = await approval_service.list(context)
        approval = next(item for item in approvals if item.run_id == trigger_run_id)
        adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
        async with adapter.checkpointer() as checkpointer:
            checkpoint = await checkpointer.aget_tuple(
                adapter.config_for_run(UUID(context.workspace_id), trigger_run_id)
            )
        assert checkpoint is not None
        checkpoint_summary_binding = checkpoint.checkpoint["channel_values"].get(
            "summary_revision_binding"
        )
        assert isinstance(checkpoint_summary_binding, dict)
        await approval_service.decide(
            context,
            approval.id,
            decision=ApprovalDecisionStatus.APPROVED,
        )
        resume_result = await run_service.resume(
            context,
            run_id=trigger_run_id,
            approval_id=approval.id,
        )
        assert resume_result.status == "SUCCEEDED"

    next_input = "NEXT-RUN-05: consume the prior fixed summary revision."
    next_run = await thread_service.submit_turn(
        context,
        thread.id,
        user_input=next_input,
        client_token=f"summary-graph-next-{uuid4().hex[:12]}",
    )

    run_ids = [item.run_id for item in (*submitted, next_run)]
    async with db_factory() as session:
        persisted_runs = tuple(
            await session.scalars(select(AgentRun).where(AgentRun.id.in_(run_ids)))
        )
        run_steps = tuple(
            await session.scalars(
                select(RunStep)
                .where(RunStep.agent_run_id.in_(run_ids))
                .order_by(RunStep.created_at)
            )
        )
        summary_jobs = tuple(
            await session.scalars(
                select(ThreadSummaryJob).where(
                    ThreadSummaryJob.workspace_id == UUID(context.workspace_id),
                    ThreadSummaryJob.thread_id == thread.id,
                )
            )
        )
    runtime_trace = {
        "runs": [(str(run.id), run.status, run.failure_code) for run in persisted_runs],
        "steps": [
            (str(step.agent_run_id), step.kind, step.status, step.safe_metadata)
            for step in run_steps
        ],
        "summary_jobs": [(str(job.id), job.status, job.failure_code) for job in summary_jobs],
    }

    assert len(provider.summary_requests) == 1, (
        f"expected one summary request, received {len(provider.summary_requests)}; "
        f"provider complete calls={len(provider.complete_requests)}; "
        f"main requests={len(provider.main_requests)}; dispatch trace={dispatch_trace}; "
        f"runtime trace={runtime_trace}"
    )
    assert len(provider.main_requests) == (6 if checkpoint_resume else 5)
    assert len(dispatch_trace["summary_budget"]) == 1
    assert dispatch_trace["summary_budget"][0][0] == "reserved"
    assert any(item[0] == "summary" and item[1] == "response" for item in dispatch_trace["gateway"])
    summary_request = provider.summary_requests[0]
    assert summary_request.request_mode.value == "provider_default"
    actual_request = provider.main_requests[-1]
    assert actual_request.request_mode.value == "provider_default"
    actual_text = "\n".join(message.content or "" for message in actual_request.messages)
    actual_summary_payload = json.loads(_summary_message(actual_request))
    assert any(
        fact.get("text") == protected_user_text
        for fact in actual_summary_payload["server_protected_facts"]
    )
    assert provider.long_answer not in actual_text
    retained_texts = [
        "OLDER-PREFIX-02: this completed exchange precedes the recent tail.",
        "RECENT-TURN-03: keep this exact user utterance.",
        "ANSWER::RECENT-TURN-03: keep this exact user utterance.",
        "RECENT-TURN-04: keep this exact user utterance too.",
        next_input,
    ]
    if not checkpoint_resume:
        retained_texts.append("ANSWER::RECENT-TURN-04: keep this exact user utterance too.")
    for retained_text in retained_texts:
        assert retained_text in actual_text

    async with db_factory() as session:
        version = await session.scalar(
            select(AgentVersion).where(AgentVersion.id == base["version"].id)
        )
        runs = tuple(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.id.in_([item.run_id for item in (*submitted, next_run)])
                )
            )
        )
        jobs = tuple(
            await session.scalars(
                select(ThreadSummaryJob).where(
                    ThreadSummaryJob.workspace_id == UUID(context.workspace_id),
                    ThreadSummaryJob.thread_id == thread.id,
                )
            )
        )
        assert version is not None
        assert len(runs) == 5 and all(run.status == "SUCCEEDED" for run in runs)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.status == "PUBLISHED"
        assert job.trigger_run_id == submitted[3].run_id
        assert job.policy_spec["request_strategy_version"] == "summary_request_strategy_v1"
        assert job.policy_spec["request_mode"] == "provider_default"
        artifact = await session.get(ThreadSummaryArtifact, job.artifact_id)
        assert artifact is not None and artifact.publication_status == "PUBLISHED"
        assert artifact.summary_content["goal"]["text"] in actual_text
        adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
        async with adapter.checkpointer() as checkpointer:
            next_checkpoint = await checkpointer.aget_tuple(
                adapter.config_for_run(UUID(context.workspace_id), next_run.run_id)
            )
        assert next_checkpoint is not None
        next_binding = next_checkpoint.checkpoint["channel_values"].get("summary_revision_binding")
        assert isinstance(next_binding, dict)
        assert next_binding["artifact_id"] == str(artifact.id)
        assert next_binding["revision_hash"] == artifact.revision_hash
        summary_steps = tuple(
            await session.scalars(
                select(RunStep).where(
                    RunStep.workspace_id == UUID(context.workspace_id),
                    RunStep.agent_run_id == submitted[3].run_id,
                    RunStep.kind == "SUMMARY",
                )
            )
        )
        assert len(summary_steps) == 1
        assert summary_steps[0].status == "SUCCEEDED"
        assert summary_steps[0].safe_metadata["summary_revision_hash"] == artifact.revision_hash
        if checkpoint_resume:
            assert checkpoint_summary_binding["artifact_id"] == str(artifact.id)
            assert checkpoint_summary_binding["revision_hash"] == artifact.revision_hash
            assert checkpoint_summary_binding["request_mode"] == "provider_default"
            assert (
                _summary_content(provider.main_requests[3])
                == _summary_content(provider.main_requests[4])
                == _summary_content(provider.main_requests[5])
            )
        summary_attempts = tuple(
            await session.scalars(
                select(RunBudgetAttempt).where(
                    RunBudgetAttempt.workspace_id == UUID(context.workspace_id),
                    RunBudgetAttempt.run_id == submitted[3].run_id,
                    RunBudgetAttempt.attempt_kind == RunBudgetAttemptKind.SUMMARY.value,
                )
            )
        )
        assert len(summary_attempts) == 1
    next_model_step = next(
        step for step in run_steps if step.agent_run_id == next_run.run_id and step.kind == "MODEL"
    )
    assert next_model_step.safe_metadata["truncated"] is False
    assert next_model_step.safe_metadata["dropped_exchange_count"] == 0
    print(
        "M4_SUMMARY_E2E "
        + json.dumps(
            {
                "scenario": "checkpoint_resume" if checkpoint_resume else "next_run",
                "summary_provider_calls": len(provider.summary_requests),
                "summary_model_request_mode": summary_request.request_mode.value,
                "summary_run_budget": dispatch_trace["summary_budget"][0][0],
                "job_id": str(job.id),
                "job_status": job.status,
                "artifact_id": str(artifact.id),
                "artifact_status": artifact.publication_status,
                "revision_hash": artifact.revision_hash,
                "trigger_run_id": str(job.trigger_run_id),
                "next_run_id": str(next_run.run_id),
                "main_model_requests": len(provider.main_requests),
                "next_request_revision_hash": next_binding["revision_hash"],
                "checkpoint_resume_revision_hash": (
                    checkpoint_summary_binding["revision_hash"] if checkpoint_resume else None
                ),
                "resume_status": resume_result.status if resume_result is not None else None,
                "run_statuses": [run.status for run in runs],
            },
            sort_keys=True,
        )
    )
