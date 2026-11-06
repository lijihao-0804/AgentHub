"""Seed a controlled engineering-conformance experiment for the portfolio demo.

The deterministic driver supplies controlled case observations, without calling
models or business tools. Dataset publication,
identity freezing, the experiment runner, persistence, metrics, and API RBAC all
use the product services. Results are explicitly engineering evidence only and
must never be presented as model-quality evidence.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from benchmarks.evaluation.dataset_builder import SCHEMA_VERSION as DATASET_SCHEMA_VERSION
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import AgentVersion
from packages.control_plane.rbac import EVALUATION_MANAGE, EVALUATION_RUN
from packages.control_plane.services import TenantService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import PrincipalContext, WorkspaceExecutionContext
from packages.evaluation.evidence_view import EvaluationEvidenceViewService
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationDatasetVersionStatus,
    EvaluationEvidenceLevel,
    EvaluationExperimentCaseResult,
    EvaluationExperimentPurpose,
    EvaluationExperimentRun,
    EvaluationExperimentRunStatus,
)
from packages.evaluation.pricing import FALLBACK_PRICING_REQUEST_KEY
from packages.evaluation.reproducibility import EVALUATION_SCHEMA_VERSION
from packages.evaluation.runner import (
    CaseExecutionObservation,
    DeterministicEvaluationDriver,
    ExperimentRunner,
)
from packages.evaluation.service import EvaluationDatasetService
from packages.evaluation.validation import validate_dataset_items

_DRIVER_VERSION = "portfolio-controlled-conformance-v1"
PRIVATE_SENTINEL = "PORTFOLIO_PRIVATE_SENTINEL_DO_NOT_SERIALIZE"
_FIXTURE_KIND = "DETERMINISTIC_ENGINEERING_CONFORMANCE"
_PASS_CASE = "portfolio-controlled-fixture-pass-v1"
_FAIL_CASE = "portfolio-controlled-fixture-fail-v1"
_UNKNOWN_CASE = "portfolio-controlled-fixture-unknown-v1"
_EXPECTED_OUTCOMES = {"PASS": 1, "FAIL": 1, "UNKNOWN": 1}
_FIXTURE_INTERPRETATIONS = {
    _PASS_CASE: "controlled_fixture_conforms",
    _FAIL_CASE: "intentional_fixture_mismatch_not_model_output",
    _UNKNOWN_CASE: "required_observation_intentionally_withheld",
}


class PortfolioConformanceDriver(DeterministicEvaluationDriver):
    """Emit three explicit, controlled observations without provider calls."""

    async def execute(
        self,
        session: AsyncSession | None = None,
        *,
        run: Any,
        variant: Any,
        item: EvaluationDatasetItem,
    ) -> CaseExecutionObservation:
        if item.case_key == _PASS_CASE:
            return await super().execute(
                session,
                run=run,
                variant=variant,
                item=item,
            )

        observation: dict[str, Any] = {
            "category": "TOOL",
            "input_hash": canonical_json_hash(item.input),
            "variant_hash": variant.variant_hash,
            "fixture_kind": _FIXTURE_KIND,
            "non_model_quality": True,
            "prompt": PRIVATE_SENTINEL,
            "private_facts": {"marker": PRIVATE_SENTINEL},
        }
        if item.case_key == _FAIL_CASE:
            # This is an intentional fixture mismatch. It is not an observed
            # response from the configured model and cannot support a model claim.
            failed_identity = "portfolio.fixture.intentional-mismatch"
            observation.update(
                {
                    "tool_identity": failed_identity,
                    "arguments": {
                        "fixture_case": "fail",
                        "result": "intentional-controlled-mismatch",
                    },
                    "tool_sequence": [failed_identity],
                    "fixture_error": "CONTROLLED_FIXTURE_EXPECTED_MISMATCH",
                }
            )
        elif item.case_key == _UNKNOWN_CASE:
            # The deterministic evaluator must derive NOT_AVAILABLE from the
            # missing argument and sequence signals; no success value is injected.
            observation.update(
                {
                    "tool_identity": item.expected["tool_identity"],
                    "fixture_observation_status": "MISSING_REQUIRED_SIGNALS",
                }
            )
        else:
            raise AgentHubError(
                "PORTFOLIO_FIXTURE_CASE_UNEXPECTED",
                "The controlled evaluation plan contains an unknown fixture case.",
                500,
            )
        return CaseExecutionObservation(observation=observation, latency_ms=0)


def _fixture_cases() -> list[dict[str, Any]]:
    cases = [
        {
            "case_key": _PASS_CASE,
            "split": "DEV",
            "category": "TOOL",
            "input": {"request": "Controlled fixture: select the declared deterministic tool."},
            "expected": {
                "tool_identity": "portfolio.fixture.pass",
                "arguments": {"fixture_case": "pass", "mode": "deterministic"},
                "tool_sequence": ["portfolio.fixture.pass"],
            },
            "tags": [
                "portfolio",
                "controlled-fixture",
                "deterministic-conformance",
                "non-model-quality",
                "expected-pass",
            ],
            "source_provenance": {
                "source_kind": "portfolio_controlled_fixture",
                "source_id": "portfolio-controlled-v1:pass",
                "fixture_result": "PASS",
                "non_model_quality": True,
            },
            "ordinal": 0,
        },
        {
            "case_key": _FAIL_CASE,
            "split": "DEV",
            "category": "TOOL",
            "input": {"request": "Controlled fixture: demonstrate an expected task mismatch."},
            "expected": {
                "tool_identity": "portfolio.fixture.fail.expected",
                "arguments": {"fixture_case": "fail", "expected": "match"},
                "tool_sequence": [
                    "portfolio.fixture.fail.expected",
                    "portfolio.fixture.fail.confirm",
                ],
            },
            "tags": [
                "portfolio",
                "controlled-fixture",
                "deterministic-conformance",
                "non-model-quality",
                "intentional-task-failure",
            ],
            "source_provenance": {
                "source_kind": "portfolio_controlled_fixture",
                "source_id": "portfolio-controlled-v1:fail",
                "fixture_result": "FAIL",
                "fixture_error": "CONTROLLED_FIXTURE_EXPECTED_MISMATCH",
                "non_model_quality": True,
            },
            "ordinal": 1,
        },
        {
            "case_key": _UNKNOWN_CASE,
            "split": "DEV",
            "category": "TOOL",
            "input": {"request": "Controlled fixture: withhold the required observation signal."},
            "expected": {
                "tool_identity": "portfolio.fixture.unknown",
                "arguments": {"fixture_case": "unknown", "required": "withheld"},
                "tool_sequence": ["portfolio.fixture.unknown"],
            },
            "tags": [
                "portfolio",
                "controlled-fixture",
                "deterministic-conformance",
                "non-model-quality",
                "unknown-observation",
            ],
            "source_provenance": {
                "source_kind": "portfolio_controlled_fixture",
                "source_id": "portfolio-controlled-v1:unknown",
                "fixture_result": "UNKNOWN",
                "observation_status": "MISSING_REQUIRED_SIGNALS",
                "non_model_quality": True,
            },
            "ordinal": 2,
        },
    ]
    return validate_dataset_items(cases)


async def _resolve_owner_context(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: UUID,
) -> WorkspaceExecutionContext:
    operation_id = uuid4().hex
    access = await TenantService().get_workspace_access(
        session,
        principal=PrincipalContext(
            request_id=f"portfolio-evaluation-{operation_id}",
            trace_id=f"portfolio-evaluation-{operation_id}",
            user_id=str(user_id),
        ),
        workspace_id=workspace_id,
    )
    missing = sorted({EVALUATION_MANAGE, EVALUATION_RUN}.difference(access.context.permissions))
    if missing:
        raise AgentHubError(
            "PORTFOLIO_EVALUATION_PERMISSION_REQUIRED",
            "The current user cannot create and execute an evaluation experiment.",
            403,
        )
    return access.context


async def _load_agent_version(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    agent_version_id: UUID,
) -> AgentVersion:
    version = await session.scalar(
        select(AgentVersion).where(
            AgentVersion.workspace_id == workspace_id,
            AgentVersion.id == agent_version_id,
        )
    )
    if version is None:
        raise AgentHubError(
            "PORTFOLIO_AGENT_VERSION_NOT_FOUND",
            "The requested AgentVersion is not in the current workspace.",
            404,
        )
    return version


async def _create_zero_priced_snapshots(
    session: AsyncSession,
    *,
    context: WorkspaceExecutionContext,
    model_plan: Any,
    operation_id: str,
) -> tuple[Any, ...]:
    effective_at = datetime.now(UTC)
    snapshots = []
    for index, profile in enumerate(model_plan.chain):
        snapshot = await EvaluationDatasetService().create_pricing_snapshot(
            session,
            context=context,
            name=f"Portfolio deterministic zero price {index + 1} {operation_id}",
            provider=profile.provider,
            model=profile.model,
            currency="USD",
            input_price_per_1m=Decimal("0"),
            output_price_per_1m=Decimal("0"),
            cached_input_price_per_1m=Decimal("0"),
            effective_at=effective_at,
            source_note=(
                "Controlled engineering-conformance fixture. Explicitly zero-priced; "
                "no model provider call or billing is enabled."
            ),
        )
        snapshots.append(snapshot)
    return tuple(snapshots)


def _task_projection(task_success: Any) -> dict[str, Any]:
    if not isinstance(task_success, Mapping):
        return {"status": "NOT_AVAILABLE", "value": None, "reason": "missing_summary"}
    status = str(task_success.get("status") or "NOT_AVAILABLE")
    value = task_success.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        value = None
    reason = task_success.get("reason")
    return {
        "status": status,
        "value": value,
        "reason": str(reason) if reason is not None else None,
    }


def _task_outcome(task: Mapping[str, Any]) -> str:
    if task.get("status") == "AVAILABLE":
        if task.get("value") == 1:
            return "PASS"
        if task.get("value") == 0:
            return "FAIL"
    return "UNKNOWN"


async def seed(
    factory,
    workspace_id: UUID,
    user_id: UUID,
    agent_version_id: UUID,
) -> dict:
    """Create and execute one isolated deterministic conformance experiment."""

    from scripts.portfolio_runtime import ROOT

    code_paths = ["apps", "packages", "scripts", "benchmarks"]
    dirty = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", *code_paths], cwd=ROOT, check=False
    ).returncode
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *code_paths],
        cwd=ROOT,
        text=True,
    )
    if dirty or untracked.strip():
        raise AgentHubError(
            "PORTFOLIO_SOURCE_NOT_FROZEN",
            "Commit the demo source before binding a formal experiment build SHA.",
            409,
        )
    operation_id = uuid4().hex
    cases = _fixture_cases()

    async with factory() as session:
        context = await _resolve_owner_context(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        version = await _load_agent_version(
            session,
            workspace_id=workspace_id,
            agent_version_id=agent_version_id,
        )
        parsed = parse_frozen_agent_spec(version.resolved_spec, workspace_id=workspace_id)

        dataset_service = EvaluationDatasetService()
        dataset = await dataset_service.create_dataset(
            session,
            context=context,
            name=f"Portfolio controlled engineering conformance {operation_id}",
            description=(
                "Deterministic engineering-conformance evidence only; not model quality. "
                "No paid model call, holdout exposure, research permit, or attempt budget."
            ),
        )
        dataset_version = await dataset_service.create_version(
            session,
            context=context,
            dataset_id=dataset.id,
            items=cases,
            schema_version=DATASET_SCHEMA_VERSION,
        )
        dataset_version = await dataset_service.publish_version(
            session,
            context=context,
            dataset_id=dataset.id,
            version_id=dataset_version.id,
        )

        pricing_snapshots = await _create_zero_priced_snapshots(
            session,
            context=context,
            model_plan=parsed.model_plan,
            operation_id=operation_id,
        )
        if not pricing_snapshots:
            raise AgentHubError(
                "PORTFOLIO_EVALUATION_PRICING_EMPTY",
                "The AgentVersion has no model identity to bind.",
                422,
            )

        experiment_service = ExperimentService()
        experiment = await experiment_service.create_experiment(
            session,
            context=context,
            name=f"Portfolio deterministic run {operation_id}",
            description=(
                "Controlled executor fixture for PASS, expected fixture mismatch, and "
                "missing-observation UNKNOWN. This is not a model-quality experiment."
            ),
            dataset_version_id=dataset_version.id,
            split="DEV",
            purpose=EvaluationExperimentPurpose.DEVELOPMENT.value,
            repetitions=1,
            evidence_level=EvaluationEvidenceLevel.DETERMINISTIC_CONFORMANCE.value,
        )
        variant = await experiment_service.add_variant(
            session,
            context=context,
            experiment_id=experiment.id,
            label="controlled-conformance-v1",
            agent_version_id=version.id,
            pricing_snapshot_id=pricing_snapshots[0].id,
            ordinal=0,
            variant_metadata={
                "fixture_kind": _FIXTURE_KIND,
                "non_model_quality": True,
                "provider_calls": "NOT_DISPATCHED",
                FALLBACK_PRICING_REQUEST_KEY: [
                    str(snapshot.id) for snapshot in pricing_snapshots[1:]
                ],
            },
        )
        experiment = await experiment_service.finalize_experiment(
            session,
            context=context,
            experiment_id=experiment.id,
        )
        run, holdout_exposure_index = await experiment_service.create_run(
            session,
            context=context,
            experiment_id=experiment.id,
        )
        if holdout_exposure_index is not None or run.split != "DEV":
            raise AgentHubError(
                "PORTFOLIO_EVALUATION_HOLDOUT_FORBIDDEN",
                "The portfolio conformance seed must remain a DEV-only experiment.",
                409,
            )

        dataset_id = dataset.id
        dataset_version_id = dataset_version.id
        dataset_version_number = dataset_version.version_number
        dataset_content_hash = dataset_version.content_hash
        experiment_id = experiment.id
        variant_id = variant.id
        variant_hash = variant.variant_hash
        build_sha = experiment.build_sha
        run_id = run.id
        pricing_ids = tuple(snapshot.id for snapshot in pricing_snapshots)
        model_provider = parsed.model_plan.primary.provider
        model_name = parsed.model_plan.primary.model

    await ExperimentRunner(
        factory,
        driver=PortfolioConformanceDriver(),
    ).execute(
        run_id=run_id,
        owner=f"portfolio-evaluation-{operation_id}",
        settings=Settings(testing=True),
    )

    async with factory() as session:
        run = await session.scalar(
            select(EvaluationExperimentRun).where(
                EvaluationExperimentRun.workspace_id == workspace_id,
                EvaluationExperimentRun.id == run_id,
            )
        )
        if run is None:
            raise AgentHubError(
                "PORTFOLIO_EVALUATION_RUN_NOT_FOUND",
                "The executed evaluation run could not be read.",
                409,
            )
        if run.status != EvaluationExperimentRunStatus.SUCCEEDED:
            raise AgentHubError(
                "PORTFOLIO_EVALUATION_EXECUTION_BLOCKED",
                "At least one controlled fixture case did not complete through the runner.",
                409,
            )

        metrics = await EvaluationMetricsService().materialize_metrics(
            session,
            context=context,
            run_id=run_id,
        )
        case_rows = list(
            await session.scalars(
                select(EvaluationExperimentCaseResult)
                .where(
                    EvaluationExperimentCaseResult.workspace_id == workspace_id,
                    EvaluationExperimentCaseResult.experiment_run_id == run_id,
                )
                .order_by(
                    EvaluationExperimentCaseResult.created_at,
                    EvaluationExperimentCaseResult.id,
                )
            )
        )
        dataset_items = {
            item.id: item
            for item in await session.scalars(
                select(EvaluationDatasetItem).where(
                    EvaluationDatasetItem.workspace_id == workspace_id,
                    EvaluationDatasetItem.dataset_version_id == dataset_version_id,
                )
            )
        }
        summaries = await EvaluationEvidenceViewService().summarize_case_results(
            session,
            context=context,
            run_id=run_id,
            cases=case_rows,
        )

        public_cases = []
        outcome_counts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
        for row in sorted(
            case_rows,
            key=lambda case: dataset_items[case.dataset_item_id].ordinal,
        ):
            item = dataset_items[row.dataset_item_id]
            task = _task_projection(summaries.get(row.id, {}).get("task_success"))
            outcome = _task_outcome(task)
            outcome_counts[outcome] += 1
            public_cases.append(
                {
                    "case_key": item.case_key,
                    "case_id": str(row.id),
                    "dataset_item_id": str(item.id),
                    "category": item.category,
                    "execution_status": str(row.status),
                    "task_success": task,
                    "task_outcome": outcome,
                    "interpretation": _FIXTURE_INTERPRETATIONS[item.case_key],
                }
            )

        if outcome_counts != _EXPECTED_OUTCOMES:
            raise AgentHubError(
                "PORTFOLIO_EVALUATION_OUTCOME_MISMATCH",
                "The persisted deterministic fixture did not produce one PASS, FAIL, and UNKNOWN.",
                409,
            )

        return {
            "run_id": str(run_id),
            "case_ids": [case["case_id"] for case in public_cases],
            "dataset_version_id": str(dataset_version_id),
            "kind": _FIXTURE_KIND,
            "deterministic": True,
            "non_model_quality": True,
            "provider_calls": "NOT_DISPATCHED",
            "evidence_level": EvaluationEvidenceLevel.DETERMINISTIC_CONFORMANCE.value,
            "execution_version": {
                "build_sha": build_sha,
                "driver_version": _DRIVER_VERSION,
                "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
            },
            "model_binding": {
                "provider": model_provider,
                "model": model_name,
            },
            "dataset": {
                "dataset_id": str(dataset_id),
                "version_id": str(dataset_version_id),
                "version_number": dataset_version_number,
                "status": EvaluationDatasetVersionStatus.PUBLISHED.value,
                "schema_version": DATASET_SCHEMA_VERSION,
                "content_hash": dataset_content_hash,
                "case_count": len(public_cases),
            },
            "experiment": {
                "experiment_id": str(experiment_id),
                "split": "DEV",
                "purpose": EvaluationExperimentPurpose.DEVELOPMENT.value,
                "variant_id": str(variant_id),
                "variant_hash": variant_hash,
            },
            "pricing": {
                "primary_snapshot_id": str(pricing_ids[0]),
                "all_snapshot_ids": [str(snapshot_id) for snapshot_id in pricing_ids],
                "explicit_zero_price": True,
            },
            "run": {
                "status": str(run.status),
                "metrics_snapshot_id": str(metrics["snapshot_id"]),
                "metrics_hash": metrics["metrics_hash"],
                "outcome_counts": outcome_counts,
            },
            "cases": public_cases,
        }


__all__ = [
    "PortfolioConformanceDriver",
    "seed",
]
