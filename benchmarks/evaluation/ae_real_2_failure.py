"""AE-Real-2: controlled failure/recovery and unknown-outcome experiment.

Real model + real runtime + real approvals, with two *controlled* fault injections:
- query_customer fails once, then succeeds (recovery evidence);
- create_ticket returns UNKNOWN_OUTCOME after approval (must not be retried blindly).

The fault injection is explicit and marked as CONTROLLED_FAULT_INJECTION, not a real
external provider failure.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from benchmarks.evaluation.ae_real_1 import _tool_spec
from benchmarks.evaluation.fault_injection import PerRunFlakyQuery
from packages.agent_runtime.adapters.langgraph import (
    LangGraphCheckpointAdapter,
    configure_windows_asyncio_policy,
)
from packages.agent_runtime.models import Tool, ToolRevision
from packages.agent_runtime.runtime_service import AgentRunService
from packages.approvals import ApprovalService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.metrics import EvaluatorRegistry
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.runner import AgentRuntimeEvaluationDriver, ExperimentRunner
from packages.evaluation.service import EvaluationDatasetService
from packages.evaluation.statistics_service import EvaluationStatisticsService
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.tools.actions import ActionExecutionResult, ActionRegistry, ActionRuntime
from packages.tools.models import Customer
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime
from tests.integration.test_m4c_agent_runtime import _seed

ISOLATED_URL = "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub_ae_real"
MODEL = "deepseek-v4.1-flash"
PROVIDER = "openai-compatible"
BASE_URL = os.environ["AE_BASE_URL"]
API_KEY = os.environ["AE_API_KEY"]
REPETITIONS = int(os.environ.get("AE_REPETITIONS", "1"))
RUN_TAG = os.environ.get("AE_RUN_TAG", "")
OUTPUT = Path(
    "docs/milestones/ae-real-2-evidence"
    + (f"-k{REPETITIONS}" if REPETITIONS > 1 else "")
    + (f"-{RUN_TAG}" if RUN_TAG else "")
    + ".json"
)


class UnknownTicket:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context, definition, arguments, *, idempotency_key):
        del context, definition, arguments, idempotency_key
        self.calls += 1
        return ActionExecutionResult.unknown_outcome()


async def _item(case_key: str, category: str, text: str, expected: dict, ordinal: int) -> dict:
    return {
        "case_key": case_key,
        "split": "DEV",
        "category": category,
        "input": {"action" if category == "APPROVAL" else "request": text},
        "expected": expected,
        "tags": ["ae-real-2", "controlled-fault-injection"],
        "source_provenance": {
            "source_kind": "synthetic_fixture",
            "source_id": f"AE-Real-2:{case_key}",
        },
        "ordinal": ordinal,
    }


async def main() -> int:
    configure_windows_asyncio_policy()
    settings = Settings(database_url=ISOLATED_URL)
    cipher = ProviderCredentialCipher.from_settings(settings)
    engine, factory = create_database(ISOLATED_URL)
    context: WorkspaceExecutionContext | None = None
    try:
        async with factory() as session:
            base = await _seed(
                session,
                label=f"ae-real-2-{uuid4().hex}",
                tool_spec=_tool_spec(
                    "create_ticket",
                    "登记待处理工单；创建不等于完成办理。",
                    "WRITE",
                    "HIGH",
                    "ALWAYS",
                ),
            )
            workspace_id = base["workspace_id"]
            user = base["user"]
            version = base["version"]
            credential = base["credential"]
            profile = base["profile"]
            credential.provider = PROVIDER
            credential.base_url = BASE_URL
            credential.secret = API_KEY
            profile.model = MODEL
            profile.temperature = 0
            profile.max_tokens = 1200
            profile.timeout_seconds = 120
            profile.capabilities = {"tool_calling": True, "max_context_tokens": 32768}

            read_tool = Tool(workspace_id=workspace_id, name="query_customer")
            session.add(read_tool)
            await session.flush()
            read_spec = _tool_spec(
                "query_customer", "按客户编号查询本工作区客户。", "READ", "LOW", "NEVER"
            )
            read_revision = ToolRevision(
                workspace_id=workspace_id,
                tool_id=read_tool.id,
                revision_number=1,
                spec=read_spec,
                spec_hash=canonical_json_hash(read_spec),
                created_by=user.id,
            )
            session.add(read_revision)
            await session.flush()
            session.add(
                Customer(
                    workspace_id=workspace_id,
                    customer_ref="C-AE-REAL-2",
                    name="AE Real Fixture",
                    email="ae-real@example.test",
                )
            )

            spec = dict(version.resolved_spec)
            spec["model"] = {
                **spec["model"],
                "profile_id": str(profile.id),
                "credential_ref": str(credential.id),
                "provider": PROVIDER,
                "model": MODEL,
                "temperature": 0,
                "max_tokens": 1200,
                "timeout_seconds": 120,
                "capabilities": {"tool_calling": True, "max_context_tokens": 32768},
                "retry_policy": {"max_attempts": 1},
                "fallback_chain": [],
                "fallback_profiles": [],
            }
            spec["prompt"] = {
                "prompt_version": 1,
                "system_prompt": (
                    "你是隔离环境中的客服代理。用户给出客户编号时先调用 query_customer。"
                    "如果 query_customer 返回错误，必须立即再调用一次 query_customer 重试；"
                    "不要凭模型自己的描述声称成功。用户要求登记工单时调用 create_ticket，"
                    "它需要人工审批。"
                ),
            }
            spec["tools"] = [
                *spec["tools"],
                {
                    "tool_revision_id": str(read_revision.id),
                    "tool_spec_hash": read_revision.spec_hash,
                    "effect": "READ",
                    "risk_level": "LOW",
                    "approval_policy": "NEVER",
                },
            ]
            spec["runtime"] = {"max_steps": 8}
            version.resolved_spec = spec
            version.resolved_spec_hash = canonical_json_hash(spec)
            await session.commit()

            context = base["context"].model_copy(
                update={
                    "permissions": base["context"].permissions
                    | {
                        "workspace_read",
                        "evaluation_manage",
                        "evaluation_run",
                        "evaluation_read",
                        "approve_action",
                        "agent_run",
                        "tool_run",
                    }
                }
            )

            dataset_service = EvaluationDatasetService()
            dataset = await dataset_service.create_dataset(
                session,
                context=context,
                name=f"ae-real-2-{uuid4().hex[:8]}",
                description="AE-Real-2 controlled failure and unknown outcome",
            )
            items = [
                await _item(
                    "real-recovery",
                    "TOOL",
                    "客户编号 C-AE-REAL-2。只调用 query_customer；如果第一次失败，必须重试一次。",
                    {
                        "tool_identity": "query_customer",
                        "arguments": {"customer_ref": "C-AE-REAL-2"},
                    },
                    0,
                ),
                await _item(
                    "real-unknown-write",
                    "APPROVAL",
                    "客户编号 C-AE-REAL-2。请为这位客户创建工单，subject 为 AE-REAL-2-UNKNOWN。",
                    {"decision": "APPROVED"},
                    1,
                ),
            ]
            dataset_version = await dataset_service.create_version(
                session, context=context, dataset_id=dataset.id, items=items
            )
            await dataset_service.publish_version(
                session,
                context=context,
                dataset_id=dataset.id,
                version_id=dataset_version.id,
            )
            pricing = await dataset_service.create_pricing_snapshot(
                session,
                context=context,
                name="ae-real-2-alibaba",
                provider=PROVIDER,
                model=MODEL,
                currency="CNY",
                input_price_per_1m=Decimal("2"),
                output_price_per_1m=Decimal("8"),
                cached_input_price_per_1m=None,
                effective_at=datetime.now(UTC),
                source_note=(
                    "AE-Real-2 controlled fault injection; "
                    "real usage records authoritative."
                ),
            )
            experiment_service = ExperimentService()
            experiment = await experiment_service.create_experiment(
                session,
                context=context,
                name=f"ae-real-2-{uuid4().hex[:8]}",
                description="Controlled failure and unknown outcome.",
                dataset_version_id=dataset_version.id,
                split="DEV",
                purpose="DEVELOPMENT",
                repetitions=REPETITIONS,
                evidence_level="REAL_PROVIDER",
            )
            await experiment_service.add_variant(
                session,
                context=context,
                experiment_id=experiment.id,
                label="controlled-fault-runtime",
                agent_version_id=version.id,
                pricing_snapshot_id=pricing.id,
                ordinal=0,
            )
            await experiment_service.finalize_experiment(
                session, context=context, experiment_id=experiment.id
            )
            run, _ = await experiment_service.create_run(
                session, context=context, experiment_id=experiment.id
            )
            print(
                json.dumps(
                    {
                        "experiment_run_id": str(run.id),
                        "experiment_id": str(experiment.id),
                        "dataset_version_id": str(dataset_version.id),
                        "agent_version_id": str(version.id),
                        "workspace_id": str(workspace_id),
                    },
                    indent=2,
                )
            )

        flaky = PerRunFlakyQuery(failures_before_success=1)
        unknown_ticket = UnknownTicket()
        runtime = AgentRunService(
            factory,
            credential_cipher=cipher,
            thread_context_provider=SqlAlchemyThreadContextProvider(factory),
            approval_service=ApprovalService(factory),
            tool_runtime=ToolRuntime(
                session_factory=factory,
                registry=ToolRegistry(handler_overrides={"query_customer": flaky}),
            ),
            action_runtime=ActionRuntime(
                session_factory=factory,
                registry=ActionRegistry(
                    session_factory=factory, overrides={"create_ticket": unknown_ticket}
                ),
            ),
            checkpoint_adapter=LangGraphCheckpointAdapter(ISOLATED_URL),
        )

        async def context_factory(run) -> WorkspaceExecutionContext:
            assert context is not None
            # A fresh request id per prepared case gives the shared fault-injection
            # handler an independent state bucket for every case/repetition.
            case_token = uuid4().hex
            return context.model_copy(
                update={
                    "request_id": f"ae-real-2:{case_token}",
                    "trace_id": f"ae-real-2:{case_token}",
                }
            )

        async def approval_context_factory(run) -> WorkspaceExecutionContext:
            assert context is not None
            return context.model_copy(
                update={"request_id": f"ae-real-2:approval:{run.id}"}
            )

        driver = AgentRuntimeEvaluationDriver(
            runtime,
            context_factory=context_factory,
            approval_context_factory=approval_context_factory,
        )
        await ExperimentRunner(factory, driver=driver).execute(
            run_id=run.id, owner=f"ae-real-2-{uuid4().hex[:8]}", settings=settings
        )

        async with factory() as session:
            metrics_service = EvaluationMetricsService(EvaluatorRegistry())
            experiment_row = await metrics_service._load_experiment(
                session, workspace_id, experiment.id
            )
            registry = EvaluatorRegistry().for_manifest(experiment_row.evaluator_manifest)
            rows = await metrics_service._load_and_validate_case_set(
                session, workspace_id, run, experiment_row
            )
            case_results = []
            for case, item, _variant in rows:
                evaluated = registry.evaluate(item.category, item.expected, case.observation)
                case_results.append(
                    {
                        "case_key": item.case_key,
                        "category": item.category,
                        "status": case.status,
                        "observed_agent_status": case.observed_agent_status,
                        "total_tokens": case.total_tokens,
                        "cost_amount": str(case.cost_amount) if case.cost_amount else None,
                        "observation": {
                            key: case.observation.get(key)
                            for key in (
                                "proposed_tool_sequence",
                                "executed_tool_sequence",
                                "proposed_calls",
                                "tool_execute_calls",
                                "action_calls",
                                "executed_calls",
                                "execution_evidence",
                                "approval_decision",
                                "approval_facts",
                                "action_executed",
                                "unauthorized_execution",
                                "duplicate_side_effect",
                                "unknown_outcome_semantics_ok",
                                "failure_taxonomy",
                            )
                        },
                        "metrics": {
                            name: {
                                "status": value.status.value,
                                "value": float(value.value) if value.value is not None else None,
                                "reason": value.reason,
                            }
                            for name, value in sorted(evaluated.items())
                        },
                    }
                )
            statistics = await EvaluationStatisticsService().compute(
                session, context=context, run_id=run.id
            )
            evidence = {
                "status": "COMPLETED",
                "experiment_run_id": str(run.id),
                "experiment_id": str(experiment.id),
                "workspace_id": str(workspace_id),
                "agent_version_id": str(version.id),
                "model": MODEL,
                "provider": PROVIDER,
                "evidence_level": "REAL_PROVIDER",
                "fault_injection": {
                    "kind": "CONTROLLED_FAULT_INJECTION",
                    "query_customer": "first call raises SIMULATED_TOOL_FAILURE, second succeeds",
                    "create_ticket": "returns UNKNOWN_OUTCOME after approval",
                },
                "case_results": case_results,
                "statistics": statistics,
            }
            OUTPUT.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
