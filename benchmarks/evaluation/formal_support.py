"""Publish reviewed scenarios and execute frozen comparisons in the isolated database.

This experiment supplies policy evidence inline; it does not claim end-to-end RAG.
HOLDOUT is a separate explicit command and records normal persisted exposure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select

from benchmarks.evaluation.artifacts import write_json_atomic
from benchmarks.evaluation.budget import TrialBudget
from benchmarks.evaluation.live_pilot import (
    ISOLATED_URL,
    PRICE_ID,
    BudgetedAdapter,
    prompt_revision_text,
)
from benchmarks.evaluation.reviewed_support_data import formal_items, reviewed_source
from packages.agent_runtime.adapters.langgraph import (
    LangGraphCheckpointAdapter,
    configure_windows_asyncio_policy,
)
from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.control_plane.models import OrganizationMembership, User, Workspace
from packages.control_plane.services import TenantService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.evaluation.approval import EvaluationApprovalActorProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
)
from packages.evaluation.runner import ExperimentRunner
from packages.evaluation.scenario_driver import ScriptedScenarioDriver
from packages.evaluation.scenario_runner import ScenarioEvaluationDriver
from packages.evaluation.service import EvaluationDatasetService
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer

ROOT = Path("docs/reviews/evidence/mi3-mi4-20261002")


async def context_for(factory, user_id, workspace_id):
    async with factory() as session:
        return (
            await TenantService().get_workspace_access(
                session,
                principal=PrincipalContext(
                    user_id=str(user_id), request_id=uuid4().hex, trace_id=uuid4().hex
                ),
                workspace_id=workspace_id,
            )
        ).context


def assert_committed_code():
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--", "packages", "benchmarks", "apps", "tests"], text=True
    )
    if dirty.strip():
        raise ValueError("FORMAL_EXPERIMENT_REQUIRES_COMMITTED_CODE")


async def prepare(output: Path):
    assert_committed_code()
    if output.exists():
        raise ValueError("FORMAL_PLAN_ALREADY_EXISTS")
    engine, factory = create_database(ISOLATED_URL)
    source = reviewed_source()
    try:
        baseline_id = UUID(
            json.loads((ROOT / "live-pilot.json").read_text(encoding="utf-8"))["agent_version_id"]
        )
        async with factory() as session:
            baseline = await session.get(AgentVersion, baseline_id)
            if baseline is None:
                raise ValueError("FORMAL_BASELINE_MISSING")
            workspace = await session.get(Workspace, baseline.workspace_id)
            user_id, workspace_id = baseline.created_by, baseline.workspace_id
            email = f"formal-approver-{uuid4().hex}@example.test"
            approver = User(email=email, normalized_email=email, password_hash="not-used")
            session.add(approver)
            await session.flush()
            session.add(
                OrganizationMembership(
                    organization_id=workspace.organization_id, user_id=approver.id, role="ADMIN"
                )
            )
            number = await session.scalar(
                select(func.max(AgentVersion.version_number)).where(
                    AgentVersion.agent_id == baseline.agent_id
                )
            )
            versions = []
            common = (
                "你是隔离测试客服。仅按以下虚构政策回答，不要编造费用。"
                "需要登记工单时，缺客户编号先询问；有编号先查询客户，再申请创建工单。"
                "拒绝审批后不得再申请或声称创建成功。"
                "审批之外的缺失业务材料写入工单留给人工核验，不阻止登记。\n"
                + "\n".join(s["text"] for s in source["sources"])
            )
            for ordinal in range(2):
                spec = json.loads(json.dumps(baseline.resolved_spec))
                spec["prompt"] = {
                    "prompt_version": 1 + ordinal,
                    "system_prompt": common
                    + (prompt_revision_text("state-and-registration") if ordinal else ""),
                }
                version = AgentVersion(
                    workspace_id=workspace_id,
                    agent_id=baseline.agent_id,
                    version_number=number + ordinal + 1,
                    spec_schema_version=baseline.spec_schema_version,
                    resolved_spec=spec,
                    resolved_spec_hash=canonical_json_hash(spec),
                    created_by=user_id,
                )
                session.add(version)
                versions.append(version)
            await session.commit()
        context = await context_for(factory, user_id, workspace_id)
        async with factory() as session:
            datasets = EvaluationDatasetService()
            dataset = await datasets.create_dataset(
                session,
                context=context,
                name=f"support-assistant-v3-{uuid4().hex}",
                description="Assistant reviewed synthetic cases; not independent human labels.",
            )
            version = await datasets.create_version(
                session,
                context=context,
                dataset_id=dataset.id,
                items=formal_items(source),
                schema_version=2,
            )
            await datasets.publish_version(
                session, context=context, dataset_id=dataset.id, version_id=version.id
            )
            pricing = await datasets.create_pricing_snapshot(
                session,
                context=context,
                name="peak upper input2 output8",
                provider="deepseek",
                model="deepseek-flash",
                currency="CNY",
                input_price_per_1m="2",
                output_price_per_1m="8",
                cached_input_price_per_1m="2",
                effective_at=datetime(2026, 10, 2, tzinfo=UTC),
                source_note="Official peak upper estimate, not invoice; https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
            )
            service = ExperimentService()
            experiment = await service.create_experiment(
                session,
                context=context,
                name="formal-support-dev-baseline-candidate",
                description="Prompt-only comparison; all 12 fictional policies inline.",
                dataset_version_id=version.id,
                split="DEV",
                purpose="DEVELOPMENT",
                repetitions=3,
            )
            for ordinal, agent_version in enumerate(versions):
                await service.add_variant(
                    session,
                    context=context,
                    experiment_id=experiment.id,
                    label="baseline" if ordinal == 0 else "candidate",
                    agent_version_id=agent_version.id,
                    pricing_snapshot_id=pricing.id,
                    ordinal=ordinal,
                    variant_metadata={
                        "driver": "scripted-scenario-v2",
                        "policy_delivery": "inline",
                        "prompt_revision": "baseline" if ordinal == 0 else "state-and-registration",
                    },
                )
            await service.finalize_experiment(session, context=context, experiment_id=experiment.id)
            run, _ = await service.create_run(session, context=context, experiment_id=experiment.id)
            write_json_atomic(
                output,
                {
                    "status": "FROZEN_DEV_PLAN",
                    "build_sha": experiment.build_sha,
                    "workspace_id": str(workspace_id),
                    "user_id": str(user_id),
                    "dataset_version_id": str(version.id),
                    "dataset_content_hash": version.content_hash,
                    "source_content_hash": source["content_hash"],
                    "agent_version_ids": [str(v.id) for v in versions],
                    "pricing_snapshot_id": str(pricing.id),
                    "experiment_id": str(experiment.id),
                    "run_id": str(run.id),
                    "planned_cases": 240,
                    "evaluator_manifest": experiment.evaluator_manifest,
                    "holdout_consumed": False,
                    "limitations": [
                        "inline_policy_not_RAG",
                        "assistant_labels_not_human",
                        "synthetic_correlated_tasks",
                    ],
                },
            )
    finally:
        await engine.dispose()


async def execute(plan: Path, output: Path):
    assert_committed_code()
    if output.exists():
        raise ValueError("FORMAL_OUTPUT_ALREADY_EXISTS")
    frozen = json.loads(plan.read_text(encoding="utf-8"))
    current_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if current_sha != frozen["build_sha"]:
        raise ValueError("FORMAL_BUILD_IDENTITY_CHANGED")
    engine, factory = create_database(ISOLATED_URL)
    budget = TrialBudget(
        ROOT / "live-budget.json", limit_cny=Decimal("50"), price_identity=PRICE_ID
    )
    adapter = BudgetedAdapter(budget)
    settings = Settings()
    cipher = ProviderCredentialCipher.from_settings(settings)
    runtime = AgentRunService(
        factory,
        model_gateway_factory=lambda db: SqlAlchemyModelGateway(
            db, adapter=adapter, credential_cipher=cipher
        ),
        thread_context_provider=SqlAlchemyThreadContextProvider(factory),
        approval_service=ApprovalService(factory),
        action_runtime=ActionRuntime(session_factory=factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(ISOLATED_URL),
    )

    async def context_factory(run):
        return await context_for(factory, run.created_by, run.workspace_id)

    async def fixture_factory(context):
        async with factory() as session:
            customer = Customer(
                workspace_id=UUID(context.workspace_id),
                customer_ref=f"formal-{uuid4().hex}",
                name="synthetic formal fixture",
            )
            session.add(customer)
            await session.commit()
            return customer.id

    driver = ScenarioEvaluationDriver(
        ScriptedScenarioDriver(runtime, driver_kind="runtime"),
        context_factory,
        fixture_factory,
        approval_context_factory=EvaluationApprovalActorProvider(factory).context_for,
    )
    report = {"status": "RUNNING", "plan": frozen, "model": "deepseek-flash", "limit_cny": "50"}
    write_json_atomic(output, report)
    try:
        await ExperimentRunner(factory, driver=driver).execute(
            run_id=UUID(frozen["run_id"]), owner=f"formal-support-{uuid4().hex}", settings=settings
        )
        async with factory() as session:
            stored = await session.get(EvaluationExperimentRun, UUID(frozen["run_id"]))
            rows = list(
                await session.scalars(
                    select(EvaluationExperimentCaseResult)
                    .where(EvaluationExperimentCaseResult.experiment_run_id == stored.id)
                    .order_by(EvaluationExperimentCaseResult.created_at)
                )
            )
            cases = []
            for row in rows:
                item = await session.get(EvaluationDatasetItem, row.dataset_item_id)
                outputs = []
                for turn in row.observation.get("turns", []):
                    agent_run = await session.get(AgentRun, UUID(turn["run_id"]))
                    outputs.append(agent_run.final_output if agent_run else None)
                cases.append(
                    {
                        "id": str(row.id),
                        "case_id": item.case_key,
                        "variant_id": str(row.experiment_variant_id),
                        "repetition": row.repetition_index,
                        "status": row.status,
                        "failure_code": row.failure_code,
                        "observation": row.observation,
                        "outputs": outputs,
                        "cost_amount": str(row.cost_amount)
                        if row.cost_amount is not None
                        else None,
                        "cost_currency": row.cost_currency,
                    }
                )
            report.update(status=stored.status, cases=cases)
    finally:
        report.update(
            calls=adapter.calls,
            local_failures=adapter.local_failures,
            allocated_cny=str(budget.allocated),
        )
        write_json_atomic(output, report)
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute"])
    parser.add_argument("--plan", type=Path, default=ROOT / "formal-support-plan.json")
    parser.add_argument("--output", type=Path, default=ROOT / "formal-support-dev.json")
    args = parser.parse_args()
    configure_windows_asyncio_policy()
    asyncio.run(prepare(args.plan) if args.action == "prepare" else execute(args.plan, args.output))
