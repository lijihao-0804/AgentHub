"""AE-Real-1: one real Agent Runtime evaluation experiment.

This is intentionally small: two APPROVAL cases on an isolated database and an
isolated workspace.  It uses the real Alibaba MaaS OpenAI-compatible endpoint, the
real Agent Runtime (tools + ApprovalService + checkpoint), the formal Experiment
service, the real AgentRuntimeEvaluationDriver, and persisted evaluation results.

Usage:
    python benchmarks/evaluation/ae_real_1.py
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

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
from packages.evaluation.runner import AgentRuntimeEvaluationDriver, ExperimentRunner
from packages.evaluation.service import EvaluationDatasetService
from packages.evaluation.statistics_service import EvaluationStatisticsService
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer, Ticket
from tests.integration.test_m4c_agent_runtime import _seed

ISOLATED_URL = "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub_ae_real"
MODEL = "deepseek-v4.1-flash"
PROVIDER = "openai-compatible"
BASE_URL = os.environ["AE_BASE_URL"]
API_KEY = os.environ["AE_API_KEY"]
OUTPUT = Path("docs/milestones/ae-real-1-evidence.json")


def _tool_spec(identity: str, description: str, effect: str, risk: str, approval: str) -> dict:
    properties = (
        {"customer_ref": {"type": "string"}}
        if identity == "query_customer"
        else {"customer_ref": {"type": "string"}, "subject": {"type": "string"}}
    )
    return {
        "kind": "builtin",
        "identity": identity,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": (
                ["customer_ref"]
                if identity == "query_customer"
                else ["customer_ref", "subject"]
            ),
            "additionalProperties": False,
        },
        "effect": effect,
        "risk_level": risk,
        "approval_policy": approval,
    }


async def _tool_dataset_item() -> dict:
    return {
        "case_key": "real-tool-query",
        "split": "DEV",
        "category": "TOOL",
        "input": {
            "request": (
                "客户编号 C-AE-REAL-1。只调用 query_customer 查询这位客户，"
                "不要创建工单，也不要调用其他工具。"
            )
        },
        "expected": {
            "tool_identity": "query_customer",
            "arguments": {"customer_ref": "C-AE-REAL-1"},
            "tool_sequence": ["query_customer"],
        },
        "tags": ["ae-real-1"],
        "source_provenance": {
            "source_kind": "synthetic_fixture",
            "source_id": "AE-Real-1:real-tool-query",
        },
        "ordinal": 2,
    }


async def _dataset_item(case_key: str, subject: str, decision: str) -> dict:
    return {
        "case_key": case_key,
        "split": "DEV",
        "category": "APPROVAL",
        "input": {
            "action": (
                f"客户编号 C-AE-REAL-1。请先用 query_customer 查询客户，"
                f"然后为这位客户创建一个待处理工单，subject 必须严格等于 {subject!r}。"
                "不要编造其它内容。"
            )
        },
        "expected": {
            "decision": decision,
        },
        "tags": ["ae-real-1"],
        "source_provenance": {
            "source_kind": "synthetic_fixture",
            "source_id": f"AE-Real-1:{case_key}",
        },
        "ordinal": 0 if decision == "APPROVED" else 1,
    }


async def main() -> int:
    configure_windows_asyncio_policy()
    settings = Settings(database_url=ISOLATED_URL)
    cipher = ProviderCredentialCipher.from_settings(settings)
    engine, factory = create_database(ISOLATED_URL)
    context: WorkspaceExecutionContext | None = None
    try:
        async with factory() as session:
            create_ticket = _tool_spec(
                "create_ticket",
                "登记待处理工单；创建不等于完成办理。",
                "WRITE",
                "HIGH",
                "ALWAYS",
            )
            base = await _seed(session, label=f"ae-real-1-{uuid4().hex}", tool_spec=create_ticket)
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
                "query_customer",
                "按客户编号查询本工作区客户。",
                "READ",
                "LOW",
                "NEVER",
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

            subject_approved = f"AE-REAL-APPROVED-{uuid4().hex[:8]}"
            subject_denied = f"AE-REAL-DENIED-{uuid4().hex[:8]}"
            session.add(
                Customer(
                    workspace_id=workspace_id,
                    customer_ref="C-AE-REAL-1",
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
                    "你是隔离环境中的客服代理。用户给出客户编号时，先调用 query_customer。"
                    "用户要求登记工单时，再调用 create_ticket；create_ticket 会进入人工审批。"
                    "审批被拒绝时，绝不能声称工单已创建或已办理；只说明未创建。"
                    "不要调用未发布的工具，不要编造字段。"
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
            spec["runtime"] = {"max_steps": 6}
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
                name=f"ae-real-1-{uuid4().hex[:8]}",
                description="AE-Real-1 isolated Runtime acceptance dataset",
            )
            items = [
                await _dataset_item("real-approval-approved", subject_approved, "APPROVED"),
                await _dataset_item("real-approval-denied", subject_denied, "DENIED"),
                await _tool_dataset_item(),
            ]
            dataset_version = await dataset_service.create_version(
                session,
                context=context,
                dataset_id=dataset.id,
                items=items,
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
                name="ae-real-1-alibaba",
                provider=PROVIDER,
                model=MODEL,
                currency="CNY",
                input_price_per_1m=Decimal("2"),
                output_price_per_1m=Decimal("8"),
                cached_input_price_per_1m=None,
                effective_at=datetime.now(UTC),
                source_note=(
                    "AE-Real-1 conservative upper estimate; "
                    "actual usage records are authoritative."
                ),
            )

            experiment_service = ExperimentService()
            experiment = await experiment_service.create_experiment(
                session,
                context=context,
                name=f"ae-real-1-{uuid4().hex[:8]}",
                description="Real Runtime acceptance: one approved and one denied tool action.",
                dataset_version_id=dataset_version.id,
                split="DEV",
                purpose="DEVELOPMENT",
                repetitions=1,
                evidence_level="REAL_PROVIDER",
            )
            await experiment_service.add_variant(
                session,
                context=context,
                experiment_id=experiment.id,
                label="real-runtime",
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
            run_id = run.id
            experiment_id = experiment.id
            dataset_version_id = dataset_version.id
            agent_version_id = version.id
            print(
                json.dumps(
                    {
                        "workspace_id": str(workspace_id),
                        "dataset_id": str(dataset.id),
                        "dataset_version_id": str(dataset_version_id),
                        "experiment_id": str(experiment_id),
                        "experiment_run_id": str(run_id),
                        "agent_version_id": str(agent_version_id),
                        "model": MODEL,
                        "subjects": {
                            "approved": subject_approved,
                            "denied": subject_denied,
                        },
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
            checkpoint_adapter=LangGraphCheckpointAdapter(ISOLATED_URL),
        )

        async def context_factory(run) -> WorkspaceExecutionContext:
            del run
            assert context is not None
            return context

        driver = AgentRuntimeEvaluationDriver(
            runtime,
            context_factory=context_factory,
            approval_context_factory=context_factory,
        )
        await ExperimentRunner(factory, driver=driver).execute(
            run_id=run_id,
            owner=f"ae-real-1-{uuid4().hex[:8]}",
            settings=settings,
        )

        async with factory() as session:
            from packages.evaluation.models import EvaluationExperimentCaseResult

            rows = list(
                await session.scalars(
                    select(EvaluationExperimentCaseResult)
                    .where(EvaluationExperimentCaseResult.experiment_run_id == run_id)
                    .order_by(EvaluationExperimentCaseResult.created_at)
                )
            )
            tickets = list(
                await session.scalars(
                    select(Ticket).where(Ticket.workspace_id == workspace_id)
                )
            )
            statistics = await EvaluationStatisticsService().compute(
                session, context=context, run_id=run_id
            )
            evidence = {
                "status": "COMPLETED",
                "experiment_run_id": str(run_id),
                "experiment_id": str(experiment_id),
                "dataset_version_id": str(dataset_version_id),
                "agent_version_id": str(agent_version_id),
                "model": MODEL,
                "provider": PROVIDER,
                "case_results": [
                    {
                        "case_key": row.case_execution_key,
                        "status": str(row.status),
                        "total_tokens": row.total_tokens,
                        "cost_amount": (
                            str(row.cost_amount)
                            if row.cost_amount is not None
                            else None
                        ),
                        "cost_status": (row.observation or {}).get("cost_status"),
                        "observation_keys": sorted((row.observation or {}).keys()),
                        "failure_code": row.failure_code,
                    }
                    for row in rows
                ],
                "business_outcome": {
                    "approved_ticket_count": sum(
                        1 for t in tickets if t.subject == subject_approved
                    ),
                    "denied_ticket_count": sum(
                        1 for t in tickets if t.subject == subject_denied
                    ),
                },
                "statistics": statistics,
            }
            output_data = json.dumps(evidence, ensure_ascii=False, indent=2, default=str)
            OUTPUT.write_text(output_data, encoding="utf-8")
            print(output_data)
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    configure_windows_asyncio_policy()
    raise SystemExit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
