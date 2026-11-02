"""AE-6b real interactive acceptance: dynamic multi-turn through ThreadService."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from benchmarks.evaluation.ae_real_1 import _tool_spec
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
from packages.evaluation.environment import SupportFixtureEnvironment
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.interactive_driver import InteractiveScenarioDriver
from packages.evaluation.metrics import EvaluatorRegistry
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.runner import ExperimentRunner
from packages.evaluation.scenario_driver import ScriptedScenarioDriver
from packages.evaluation.service import EvaluationDatasetService
from packages.evaluation.statistics_service import EvaluationStatisticsService
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime
from tests.integration.test_m4c_agent_runtime import _seed

ISOLATED_URL = "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub_ae_real"
MODEL = "deepseek-v4.1-flash"
PROVIDER = "openai-compatible"
BASE_URL = os.environ["AE_BASE_URL"]
API_KEY = os.environ["AE_API_KEY"]
OUTPUT = Path("docs/milestones/ae-6b-evidence.json")


async def _item(
    case_key: str,
    task: str,
    *,
    facts: dict,
    triggers: dict,
    expected: dict,
    ordinal: int,
):
    return {
        "case_key": case_key,
        "split": "DEV",
        "category": "INTERACTIVE",
        "input": {
            "task": task,
            "environment_ref": "support_customer",
            "simulator": {
                "private_facts": facts,
                "triggers": triggers,
                "fallback": "我不记得或不想提供更多信息。",
                "max_turns": 4,
            },
            "max_turns": 4,
            "max_tokens": 12000,
            "max_cost_cny": 0.20,
        },
        "expected": expected,
        "tags": ["ae-6b", "interactive"],
        "source_provenance": {
            "source_kind": "synthetic_fixture",
            "source_id": f"AE-6b:{case_key}",
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
                label=f"ae6b-{uuid4().hex}",
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
                    customer_ref="AE6B-BOOTSTRAP",
                    name="AE6B bootstrap",
                    email="ae6b@example.test",
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
                    "你是隔离环境中的客服代理。用户没有提供客户编号时，必须先向用户询问客户编号；"
                    "得到编号后先调用 query_customer。用户要求登记工单时再调用 create_ticket。"
                    "create_ticket 会进入人工审批。审批拒绝或没有实际执行时，绝不能声称已经创建。"
                    "不要编造客户编号，不要调用未发布工具。"
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
                name=f"ae6b-{uuid4().hex[:8]}",
                description="AE-6b interactive support acceptance",
            )
            items = [
                await _item(
                    "interactive-clarify-create",
                    "你好，我想办理退货登记，但我现在不记得客户编号。",
                    facts={"customer_ref": "__CUSTOMER_REF__"},
                    triggers={"customer_ref": ["客户编号", "编号", "客户号"]},
                    expected={
                        "assertions": {"ticket_count": 1, "ticket_status": "OPEN", "min_turns": 2},
                        "approval_decisions": ["APPROVED"],
                        "canary": "AE6B-CANARY-A",
                    },
                    ordinal=0,
                ),
                await _item(
                    "interactive-refuse-without-info",
                    "我想办理退货登记，但我不打算提供任何身份信息。",
                    facts={},
                    triggers={},
                    expected={
                        "assertions": {"ticket_count": 0, "min_turns": 2},
                        "canary": "AE6B-CANARY-B",
                    },
                    ordinal=1,
                ),
                await _item(
                    "interactive-denied-write",
                    "客户编号我已经给你了，请帮我登记一个工单，subject 是 AE6B-DENIED。",
                    facts={"customer_ref": "__CUSTOMER_REF__"},
                    triggers={"customer_ref": ["客户编号", "编号", "客户号"]},
                    expected={
                        "assertions": {"ticket_count": 0, "min_turns": 2},
                        "approval_decisions": ["DENIED"],
                        "canary": "AE6B-CANARY-C",
                    },
                    ordinal=2,
                ),
            ]
            dataset_version = await dataset_service.create_version(
                session, context=context, dataset_id=dataset.id, items=items, schema_version=2
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
                name="ae6b-alibaba",
                provider=PROVIDER,
                model=MODEL,
                currency="CNY",
                input_price_per_1m=Decimal("2"),
                output_price_per_1m=Decimal("8"),
                cached_input_price_per_1m=None,
                effective_at=datetime.now(UTC),
                source_note="AE-6b interactive acceptance; real usage records authoritative.",
            )
            service = ExperimentService()
            experiment = await service.create_experiment(
                session,
                context=context,
                name=f"ae6b-{uuid4().hex[:8]}",
                description="Interactive dynamic multi-turn acceptance.",
                dataset_version_id=dataset_version.id,
                split="DEV",
                purpose="DEVELOPMENT",
                repetitions=1,
                evidence_level="REAL_PROVIDER",
            )
            await service.add_variant(
                session,
                context=context,
                experiment_id=experiment.id,
                label="interactive-runtime",
                agent_version_id=version.id,
                pricing_snapshot_id=pricing.id,
                ordinal=0,
            )
            await service.finalize_experiment(
                session, context=context, experiment_id=experiment.id
            )
            run, _ = await service.create_run(session, context=context, experiment_id=experiment.id)
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

        runtime = AgentRunService(
            factory,
            credential_cipher=cipher,
            thread_context_provider=SqlAlchemyThreadContextProvider(factory),
            approval_service=ApprovalService(factory),
            action_runtime=ActionRuntime(session_factory=factory),
            tool_runtime=ToolRuntime(session_factory=factory, registry=ToolRegistry()),
            checkpoint_adapter=LangGraphCheckpointAdapter(ISOLATED_URL),
        )
        async def context_factory(run):
            del run
            return context

        async def approval_context_factory(run):
            del run
            return context

        scenarios = ScriptedScenarioDriver(runtime, driver_kind="runtime")
        driver = InteractiveScenarioDriver(
            scenarios,
            context_factory=context_factory,
            environment_factory=lambda ctx, item: SupportFixtureEnvironment(
                factory, workspace_id=ctx.workspace_id
            ),
            approval_context_factory=approval_context_factory,
        )
        await ExperimentRunner(factory, driver=driver).execute(
            run_id=run.id, owner=f"ae6b-{uuid4().hex[:8]}", settings=settings
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
            cases = []
            for case, item, _variant in rows:
                evaluated = registry.evaluate(item.category, item.expected, case.observation)
                cases.append(
                    {
                        "case_key": item.case_key,
                        "status": case.status,
                        "observed_agent_status": case.observed_agent_status,
                        "total_tokens": case.total_tokens,
                        "input_tokens": case.input_tokens,
                        "output_tokens": case.output_tokens,
                        "cost_amount": str(case.cost_amount) if case.cost_amount else None,
                        "cost_status": (
                            "KNOWN" if case.cost_amount is not None else "UNKNOWN"
                        ),
                        "estimated_cost_cny": (
                            str(
                                (Decimal(case.input_tokens or 0) * Decimal("2")
                                 + Decimal(case.output_tokens or 0) * Decimal("8"))
                                / Decimal("1000000")
                            )
                            if case.cost_amount is None
                            else None
                        ),
                        "observation": {
                            key: case.observation.get(key)
                            for key in (
                                "executed_turn_count",
                                "stop_reason",
                                "tool_sequence",
                                "environment",
                                "business_outcome",
                                "disclosure_policy_respected",
                                "canary_leaked",
                                "approval_decision_count",
                                "turns",
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
            stats = await EvaluationStatisticsService().compute(
                session, context=context, run_id=run.id
            )
            evidence = {
                "status": "COMPLETED",
                "experiment_run_id": str(run.id),
                "experiment_id": str(experiment.id),
                "dataset_version_id": str(dataset_version.id),
                "agent_version_id": str(version.id),
                "workspace_id": str(workspace_id),
                "model": MODEL,
                "provider": PROVIDER,
                "environment_kind": "support_fixture",
                "simulator_kind": "scripted-disclosure-v1",
                "cases": cases,
                "statistics": stats,
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
