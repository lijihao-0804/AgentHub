"""Execute the complete deterministic M7-H evaluation acceptance chain.

This runner is intentionally provider-free: it exercises the persisted Dataset, Experiment,
ExperimentRunner, Metrics, Comparison, Ablation, and Release Gate services with the real
PostgreSQL schema and the deterministic conformance driver.  It is not a model-quality score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benchmarks.evaluation.dataset_builder import (
    SCHEMA_VERSION,
    build_payload,
)
from benchmarks.evaluation.dataset_builder import (
    summary as dataset_summary,
)
from packages.agent_runtime.models import AgentVersion
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.evaluation.ablation import EvaluationAblationService
from packages.evaluation.build_identity import StaticBuildIdentityProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.models import (
    EvaluationDatasetVersion,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    EvaluationExperimentVariant,
)
from packages.evaluation.release_gate import EvaluationReleaseGateService
from packages.evaluation.runner import DeterministicEvaluationDriver, ExperimentRunner
from packages.evaluation.service import EvaluationDatasetService
from tests.integration.test_m4c_agent_runtime import _seed

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "benchmarks" / "evaluation" / "dataset.json"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "benchmarks" / "evaluation" / "results" / "m7-unified-conformance.json"
)


def _async_database_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+asyncpg://", 1)
    return value


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _context(base: dict[str, Any]):
    return base["context"].model_copy(
        update={
            "workspace_role": "DEVELOPER",
            "permissions": frozenset(
                {
                    "workspace_read",
                    "evaluation_read",
                    "evaluation_manage",
                    "evaluation_run",
                }
            ),
        }
    )


def _dataset_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(payload["items"])


async def _create_versions(
    session: AsyncSession, base: dict[str, Any]
) -> tuple[AgentVersion, AgentVersion]:
    baseline = base["version"]
    candidate_spec = {
        **baseline.resolved_spec,
        "prompt": {**baseline.resolved_spec["prompt"], "prompt_version": 2},
    }
    candidate = AgentVersion(
        workspace_id=baseline.workspace_id,
        agent_id=baseline.agent_id,
        version_number=baseline.version_number + 1,
        spec_schema_version=baseline.spec_schema_version,
        resolved_spec=candidate_spec,
        resolved_spec_hash=canonical_json_hash(candidate_spec),
        created_by=baseline.created_by,
    )
    session.add(candidate)
    await session.commit()
    return baseline, candidate


async def _create_pricing(session: AsyncSession, base: dict[str, Any], context):
    return await EvaluationDatasetService().create_pricing_snapshot(
        session,
        context=context,
        name=f"M7-H pricing {uuid4().hex}",
        provider="fake",
        model="frozen-model-a",
        currency="USD",
        input_price_per_1m=Decimal("0.10"),
        output_price_per_1m=Decimal("0.20"),
        cached_input_price_per_1m=None,
        effective_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        source_note="Deterministic conformance fixture; no provider billing data.",
    )


async def _prepare_experiment(
    session: AsyncSession,
    *,
    context,
    dataset_version: EvaluationDatasetVersion,
    baseline: AgentVersion,
    candidate: AgentVersion,
    pricing,
    split: str,
    purpose: str,
    build_sha: str,
) -> tuple[EvaluationExperimentRun, Any, Any]:
    service = ExperimentService(StaticBuildIdentityProvider(build_sha))
    experiment = await service.create_experiment(
        session,
        context=context,
        name=f"M7-H {split.lower()} {uuid4().hex}",
        description="Deterministic M7-H conformance run.",
        dataset_version_id=dataset_version.id,
        split=split,
        purpose=purpose,
        repetitions=1,
    )
    baseline_variant = await service.add_variant(
        session,
        context=context,
        experiment_id=experiment.id,
        label="baseline",
        agent_version_id=baseline.id,
        pricing_snapshot_id=pricing.id,
        ordinal=0,
        variant_metadata={"prompt_variant": "baseline"},
    )
    candidate_variant = await service.add_variant(
        session,
        context=context,
        experiment_id=experiment.id,
        label="candidate",
        agent_version_id=candidate.id,
        pricing_snapshot_id=pricing.id,
        ordinal=1,
        variant_metadata={"prompt_variant": "candidate"},
    )
    await service.finalize_experiment(session, context=context, experiment_id=experiment.id)
    run, exposure_index = await service.create_run(
        session, context=context, experiment_id=experiment.id
    )
    del exposure_index
    return run, baseline_variant, candidate_variant


async def _execute_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    run: EvaluationExperimentRun,
    context,
    baseline_variant: EvaluationExperimentVariant,
    candidate_variant: EvaluationExperimentVariant,
    settings: Settings,
) -> dict[str, Any]:
    await ExperimentRunner(factory, driver=DeterministicEvaluationDriver()).execute(
        run_id=run.id,
        owner=f"m7h-{run.split.lower()}-{uuid4().hex}",
        settings=settings,
    )
    async with factory() as session:
        metrics_service = EvaluationMetricsService()
        metrics = await metrics_service.materialize_metrics(session, context=context, run_id=run.id)
        comparison = await metrics_service.create_comparison(
            session,
            context=context,
            run_id=run.id,
            baseline_variant_id=baseline_variant.id,
            candidate_variant_id=candidate_variant.id,
        )
        ablation = await EvaluationAblationService().create_ablation(
            session, context=context, run_id=run.id, comparison_id=comparison.id
        )
        rows = list(
            await session.scalars(
                select(EvaluationExperimentCaseResult).where(
                    EvaluationExperimentCaseResult.experiment_run_id == run.id
                )
            )
        )
        return {
            "_comparison_id": comparison.id,
            "execution_count": len(rows),
            "metric_snapshot_hash": metrics["metrics_hash"],
            "comparison_hash": comparison.comparison_hash,
            "comparison_status": comparison.status,
            "complete_pairs": comparison.paired_pairs,
            "incomplete_pairs": comparison.missing_pairs,
            "ablation_factor": ablation.factor,
            "ablation_analysis_hash": ablation.analysis_hash,
            "case_executions": {
                "count": len(rows),
                "status_counts": dict(sorted(Counter(str(row.status) for row in rows).items())),
            },
        }


async def run_formal_chain(database_url: str, *, output: Path | None = None) -> dict[str, Any]:
    payload = build_payload()
    engine, factory = create_database(_async_database_url(database_url))
    try:
        async with factory() as session:
            base = await _seed(session, label=f"m7h-{uuid4().hex}")
            context = _context(base)
            dataset_service = EvaluationDatasetService()
            dataset = await dataset_service.create_dataset(
                session,
                context=context,
                name=f"M7-H Unified {uuid4().hex}",
                description="Frozen deterministic conformance dataset.",
            )
            dataset_version = await dataset_service.create_version(
                session,
                context=context,
                dataset_id=dataset.id,
                items=_dataset_items(payload),
                schema_version=SCHEMA_VERSION,
            )
            await dataset_service.publish_version(
                session, context=context, dataset_id=dataset.id, version_id=dataset_version.id
            )
            pricing = await _create_pricing(session, base, context)
            baseline, candidate = await _create_versions(session, base)
            build_sha = _git_commit()
            dev_run, dev_baseline, dev_candidate = await _prepare_experiment(
                session,
                context=context,
                dataset_version=dataset_version,
                baseline=baseline,
                candidate=candidate,
                pricing=pricing,
                split="DEV",
                purpose="DEVELOPMENT",
                build_sha=build_sha,
            )
            holdout_run, holdout_baseline, holdout_candidate = await _prepare_experiment(
                session,
                context=context,
                dataset_version=dataset_version,
                baseline=baseline,
                candidate=candidate,
                pricing=pricing,
                split="HOLDOUT",
                purpose="RELEASE_GATE",
                build_sha=build_sha,
            )
        settings = Settings(testing=True)
        dev = await _execute_run(
            factory,
            run=dev_run,
            context=context,
            baseline_variant=dev_baseline,
            candidate_variant=dev_candidate,
            settings=settings,
        )
        holdout = await _execute_run(
            factory,
            run=holdout_run,
            context=context,
            baseline_variant=holdout_baseline,
            candidate_variant=holdout_candidate,
            settings=settings,
        )
        async with factory() as session:
            policy = await EvaluationReleaseGateService().create_policy(
                session,
                context=context,
                name=f"M7-H deterministic no-regression {uuid4().hex}",
                description="Safety and task conformance gate; timing is intentionally excluded.",
                policy_json={
                    "rules": [
                        {
                            "metric": "task_success",
                            "rule": "NO_REGRESSION",
                            "required": True,
                            "safety": True,
                        },
                        {
                            "metric": "approval_decision_accuracy",
                            "rule": "NO_REGRESSION",
                            "required": True,
                            "safety": True,
                        },
                        {
                            "metric": "denied_action_execution_rate",
                            "rule": "NO_REGRESSION",
                            "required": True,
                            "safety": True,
                        },
                        {
                            "metric": "unauthorized_execution_rate",
                            "rule": "NO_REGRESSION",
                            "required": True,
                            "safety": True,
                        },
                        {
                            "metric": "duplicate_side_effect_rate",
                            "rule": "NO_REGRESSION",
                            "required": True,
                            "safety": True,
                        },
                    ]
                },
            )
            decision = await EvaluationReleaseGateService().create_decision(
                session,
                context=context,
                run_id=holdout_run.id,
                comparison_id=holdout["_comparison_id"],
                policy_id=policy.id,
            )
            exposure_count = await ExperimentService().holdout_exposure_count(
                session, context=context, experiment_id=holdout_run.experiment_id
            )
        result = {
            "benchmark": "m7-unified-evaluation-conformance",
            "status": "PASS" if decision.status == "PASS" else decision.status,
            "deterministic_conformance_only": True,
            "git_commit": _git_commit(),
            "dataset": dataset_summary(payload),
            "formal_pipeline": {
                "dev": dev,
                "holdout": holdout,
                "holdout_exposure_count": exposure_count,
                "release_gate": {
                    "status": decision.status,
                    "decision_hash": decision.decision_hash,
                    "policy_hash": policy.policy_hash,
                },
            },
            "real_provider_eval": "NOT_RUN",
            "real_retrieval_artifact": (
                "benchmarks/retrieval/results/"
                "m7e-retrieval-ablation-76286b751ed7bc4dd397885c6ad65eb0951b6280.json"
            ),
        }
        result["formal_pipeline"]["dev"].pop("_comparison_id", None)
        result["formal_pipeline"]["holdout"].pop("_comparison_id", None)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return result
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    expected = build_payload()
    if args.validate_only:
        actual = json.loads(args.dataset.read_text(encoding="utf-8"))
        if actual != expected:
            raise SystemExit("M7-H dataset drift detected")
        print(json.dumps(dataset_summary(expected), ensure_ascii=False, sort_keys=True))
        return 0
    database_url = args.database_url or os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("--database-url or AGENTHUB_TEST_DATABASE_URL is required")
    result = asyncio.run(run_formal_chain(database_url, output=args.output))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run_formal_chain", "main"]
