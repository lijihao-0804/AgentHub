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
    EvaluationDatasetVersion,
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


async def prepare(output: Path, reuse_plan: Path | None = None):
    assert_committed_code()
    if output.exists():
        raise ValueError("FORMAL_PLAN_ALREADY_EXISTS")
    engine, factory = create_database(ISOLATED_URL)
    source = reviewed_source()
    previous = json.loads(reuse_plan.read_text(encoding="utf-8")) if reuse_plan else None
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
                # Shared isolated ceiling below the provider's documented 1M;
                # the conservative UTF-8 estimator and cost guards stay active.
                spec["model"]["capabilities"]["max_context_tokens"] = 16384
                spec["runtime"]["context_budget"] = {
                    "reserved_output_tokens": 800,
                    "max_retrieval_tokens": 5000,
                    "max_tool_result_tokens": 4000,
                }
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
            if previous is None:
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
            else:
                if previous["source_content_hash"] != source["content_hash"] or previous[
                    "workspace_id"
                ] != str(workspace_id):
                    raise ValueError("FORMAL_REUSE_SOURCE_IDENTITY_MISMATCH")
                version = await session.get(
                    EvaluationDatasetVersion, UUID(previous["dataset_version_id"])
                )
                if (
                    version is None
                    or version.status != "PUBLISHED"
                    or version.content_hash != previous["dataset_content_hash"]
                ):
                    raise ValueError("FORMAL_REUSE_DATASET_IDENTITY_MISMATCH")
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
                    "supersedes_plan": str(reuse_plan) if reuse_plan else None,
                    "shared_context_limit": 16384,
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
    preflight = json.loads(plan.with_suffix(".preflight.json").read_text(encoding="utf-8"))
    if preflight["status"] != "PASS" or preflight["plan_hash"] != canonical_json_hash(frozen):
        raise ValueError("FORMAL_CONTEXT_PREFLIGHT_REQUIRED")
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


async def prepare_holdout(dev_plan: Path, output: Path):
    """Reuse frozen candidates and dataset; normal service persists exposure."""
    assert_committed_code()
    if output.exists():
        raise ValueError("FORMAL_PLAN_ALREADY_EXISTS")
    frozen = json.loads(dev_plan.read_text(encoding="utf-8"))
    current_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if current_sha != frozen["build_sha"]:
        raise ValueError("FORMAL_BUILD_IDENTITY_CHANGED")
    engine, factory = create_database(ISOLATED_URL)
    try:
        context = await context_for(factory, UUID(frozen["user_id"]), UUID(frozen["workspace_id"]))
        async with factory() as session:
            dev_run = await session.get(EvaluationExperimentRun, UUID(frozen["run_id"]))
            if dev_run is None or dev_run.status != "SUCCEEDED":
                raise ValueError("FORMAL_DEV_EXECUTION_NOT_COMPLETE")
            service = ExperimentService()
            experiment = await service.create_experiment(
                session,
                context=context,
                name="formal-support-holdout",
                description="Frozen candidate validation; no tuning on HOLDOUT.",
                dataset_version_id=UUID(frozen["dataset_version_id"]),
                split="HOLDOUT",
                purpose="HOLDOUT_VALIDATION",
                repetitions=3,
            )
            for ordinal, version_id in enumerate(frozen["agent_version_ids"]):
                await service.add_variant(
                    session,
                    context=context,
                    experiment_id=experiment.id,
                    label="baseline" if ordinal == 0 else "candidate",
                    agent_version_id=UUID(version_id),
                    pricing_snapshot_id=UUID(frozen["pricing_snapshot_id"]),
                    ordinal=ordinal,
                    variant_metadata={
                        "policy_delivery": "inline",
                        "driver": "scripted-scenario-v2",
                    },
                )
            await service.finalize_experiment(session, context=context, experiment_id=experiment.id)
            run, exposure = await service.create_run(
                session, context=context, experiment_id=experiment.id
            )
            write_json_atomic(
                output,
                {
                    **frozen,
                    "status": "FROZEN_HOLDOUT_PLAN",
                    "dev_plan": str(dev_plan),
                    "experiment_id": str(experiment.id),
                    "run_id": str(run.id),
                    "planned_cases": 120,
                    "holdout_consumed": True,
                    "holdout_exposure_index": exposure,
                    "evaluator_manifest": experiment.evaluator_manifest,
                },
            )
    finally:
        await engine.dispose()


async def preflight(plan: Path):
    """All first-turn mandatory contexts, with controlled gateway and no side effects."""
    from packages.evaluation.models import EvaluationExperimentVariant
    from packages.model_gateway.contracts import ModelResponse
    from tests.integration.test_m4c_agent_runtime import ScriptedGateway

    assert_committed_code()
    frozen = json.loads(plan.read_text(encoding="utf-8"))
    output = plan.with_suffix(".preflight.json")
    if output.exists():
        raise ValueError("PREFLIGHT_OUTPUT_EXISTS")
    if (
        subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        != frozen["build_sha"]
    ):
        raise ValueError("FORMAL_BUILD_IDENTITY_CHANGED")
    engine, factory = create_database(ISOLATED_URL)
    report = {
        "status": "RUNNING",
        "plan_hash": canonical_json_hash(frozen),
        "provider": "controlled",
        "scope": "first_turn_mandatory_context_admission_only",
        "cases": [],
        "paid_calls": 0,
    }
    try:
        context = await context_for(factory, UUID(frozen["user_id"]), UUID(frozen["workspace_id"]))
        gateway = ScriptedGateway(
            [ModelResponse(content="preflight fixture", provider="controlled", model="controlled")]
        )
        runtime = AgentRunService(factory, model_gateway_factory=lambda db: gateway)
        async with factory() as session:
            experiment_run = await session.get(EvaluationExperimentRun, UUID(frozen["run_id"]))
            variants = list(
                await session.scalars(
                    select(EvaluationExperimentVariant).where(
                        EvaluationExperimentVariant.experiment_id == experiment_run.experiment_id
                    )
                )
            )
            items = list(
                await session.scalars(
                    select(EvaluationDatasetItem).where(
                        EvaluationDatasetItem.dataset_version_id
                        == experiment_run.dataset_version_id,
                        EvaluationDatasetItem.split == experiment_run.split,
                    )
                )
            )
        for variant in variants:
            for item in items:
                result = await runtime.run(
                    context,
                    agent_version_id=variant.agent_version_id,
                    input_text=item.input["task"].replace(
                        "fixture_customer_ref", f"formal-{uuid4().hex}"
                    ),
                )
                report["cases"].append(
                    {
                        "case_id": item.case_key,
                        "variant_id": str(variant.id),
                        "run_id": str(result.run_id),
                        "status": result.status,
                        "failure_code": result.failure_code,
                    }
                )
        report["status"] = (
            "PASS" if all(c["status"] == "SUCCEEDED" for c in report["cases"]) else "FAIL"
        )
        if report["status"] != "PASS":
            raise ValueError("FORMAL_CONTEXT_PREFLIGHT_FAILED")
    finally:
        write_json_atomic(output, report)
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "holdout", "preflight"])
    parser.add_argument("--plan", type=Path, default=ROOT / "formal-support-plan.json")
    parser.add_argument("--output", type=Path, default=ROOT / "formal-support-dev.json")
    parser.add_argument("--reuse-plan", type=Path)
    parser.add_argument("--dev-plan", type=Path)
    args = parser.parse_args()
    configure_windows_asyncio_policy()
    if args.action == "prepare":
        asyncio.run(prepare(args.plan, args.reuse_plan))
    elif args.action == "holdout":
        if args.dev_plan is None:
            parser.error("holdout requires --dev-plan")
        asyncio.run(prepare_holdout(args.dev_plan, args.plan))
    elif args.action == "preflight":
        asyncio.run(preflight(args.plan))
    else:
        asyncio.run(execute(args.plan, args.output))
