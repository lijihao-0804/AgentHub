from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app import create_app
from packages.agent_runtime.models import AgentVersion
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.ablation import EvaluationAblationService
from packages.evaluation.build_identity import StaticBuildIdentityProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.models import EvaluationExperimentRun
from packages.evaluation.release_gate import EvaluationReleaseGateService
from packages.evaluation.runner import CaseExecutionObservation, ExperimentRunner
from packages.evaluation.service import EvaluationDatasetService
from tests.integration.test_ae10_evidence_view import _client
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m7b_experiments import (
    _item,
    _manager_context,
    _pricing,
)

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M7-E/F PostgreSQL integration tests.",
    ),
]


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


@pytest.fixture(scope="session")
def migrated_database() -> None:
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def db_factory(migrated_database: None) -> async_sessionmaker[AsyncSession]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


class _CountingDriver:
    async def execute(self, session, *, run, variant, item) -> CaseExecutionObservation:
        del session, run, variant
        return CaseExecutionObservation(
            observation={
                "category": item.category,
                "candidate_chunk_ids": list(item.expected.get("relevant_chunk_ids", [])),
                "final_chunk_ids": list(item.expected.get("relevant_chunk_ids", [])),
                "candidate_top_k": 20,
                "final_top_k": 5,
                "citation_ids": list(item.expected.get("relevant_chunk_ids", [])),
                "driver_kind": "controlled",
                "observation_schema_version": 2,
            },
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cached_tokens=0,
        )


async def _release_run(session: AsyncSession, base: dict[str, object], case_keys=("holdout",)):
    context = _manager_context(base)
    dataset = await EvaluationDatasetService().create_dataset(
        session, context=context, name=f"m7ef-{uuid4().hex}"
    )
    dataset_version = await EvaluationDatasetService().create_version(
        session,
        context=context,
        dataset_id=dataset.id,
        items=[_item(key, split="HOLDOUT", ordinal=i) for i, key in enumerate(case_keys)],
    )
    await EvaluationDatasetService().publish_version(
        session, context=context, dataset_id=dataset.id, version_id=dataset_version.id
    )
    pricing = await _pricing(session, base)
    experiment_service = ExperimentService(StaticBuildIdentityProvider("e" * 40))
    experiment = await experiment_service.create_experiment(
        session,
        context=context,
        name=f"release-{uuid4().hex}",
        description=None,
        dataset_version_id=dataset_version.id,
        split="HOLDOUT",
        purpose="RELEASE_GATE",
    )
    before = base["version"]
    assert isinstance(before, AgentVersion)
    candidate_spec = {
        **before.resolved_spec,
        "prompt": {**before.resolved_spec["prompt"], "prompt_version": 2},
    }
    candidate = AgentVersion(
        workspace_id=before.workspace_id,
        agent_id=before.agent_id,
        version_number=before.version_number + 1,
        spec_schema_version=before.spec_schema_version,
        resolved_spec=candidate_spec,
        resolved_spec_hash=canonical_json_hash(candidate_spec),
        created_by=before.created_by,
    )
    session.add(candidate)
    await session.flush()
    baseline_variant = await experiment_service.add_variant(
        session,
        context=context,
        experiment_id=experiment.id,
        label="baseline",
        agent_version_id=before.id,
        pricing_snapshot_id=pricing.id,
        ordinal=0,
    )
    candidate_variant = await experiment_service.add_variant(
        session,
        context=context,
        experiment_id=experiment.id,
        label="candidate",
        agent_version_id=candidate.id,
        pricing_snapshot_id=pricing.id,
        ordinal=1,
    )
    await experiment_service.finalize_experiment(
        session, context=context, experiment_id=experiment.id
    )
    run, _ = await experiment_service.create_run(
        session, context=context, experiment_id=experiment.id
    )
    return run, baseline_variant, candidate_variant, context


@pytest.mark.asyncio
async def test_m7ef_persisted_ablation_to_release_gate_chain_and_scope(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7ef-chain-{uuid4().hex}")
        run, baseline, candidate, context = await _release_run(session, base)
    await ExperimentRunner(db_factory, driver=_CountingDriver()).execute(
        run_id=run.id,
        owner="m7ef-worker",
        settings=Settings(testing=True),
    )

    async with db_factory() as session:
        metrics_service = EvaluationMetricsService()
        await metrics_service.materialize_metrics(session, context=context, run_id=run.id)
        comparison = await metrics_service.create_comparison(
            session,
            context=context,
            run_id=run.id,
            baseline_variant_id=baseline.id,
            candidate_variant_id=candidate.id,
        )
        settings = Settings(testing=True)
        app = create_app(settings)
        app.state.db_session_factory = db_factory
        path = (
            f"/api/v1/workspaces/{base['workspace_id']}/evaluation/experiment-runs/{run.id}"
            f"/comparisons/{comparison.id}/paired-success"
        )
        async with _client(app, base["user"].id, settings) as client:
            response = await client.get(path)
            assert response.status_code == 200, response.text
            report = response.json()
            assert report["pair_count"] == 1
            assert report["delta"] == 0
            assert report["cases"] == [
                {
                    "case_key": "holdout",
                    "delta": 0,
                    "matched_repetitions": 1,
                    "planned_repetitions": 1,
                    "baseline_missing": 0,
                    "candidate_missing": 0,
                }
            ]
            assert (await client.get(path)).json() == report
            wrong_scope = path.replace(str(base["workspace_id"]), str(uuid4()))
            assert (await client.get(wrong_scope)).status_code in {403, 404}
        ablation_service = EvaluationAblationService()
        ablation = await ablation_service.create_ablation(
            session, context=context, run_id=run.id, comparison_id=comparison.id
        )
        assert ablation.factor == "PROMPT"
        policy_service = EvaluationReleaseGateService()
        policy = await policy_service.create_policy(
            session,
            context=context,
            name=f"release-policy-{uuid4().hex}",
            description=None,
            policy_json={
                "rules": [{"metric": "task_success", "rule": "NO_REGRESSION", "safety": True}]
            },
        )
        decision = await policy_service.create_decision(
            session,
            context=context,
            run_id=run.id,
            comparison_id=comparison.id,
            policy_id=policy.id,
        )
        again = await policy_service.create_decision(
            session,
            context=context,
            run_id=run.id,
            comparison_id=comparison.id,
            policy_id=policy.id,
        )
        assert decision.status == "PASS"
        assert decision.id == again.id
        assert decision.comparison_hash == comparison.comparison_hash
        assert decision.policy_hash == policy.policy_hash

        viewer = context.model_copy(
            update={"workspace_role": "VIEWER", "permissions": frozenset({"evaluation_read"})}
        )
        assert await policy_service.get_decision(
            session,
            context=viewer,
            run_id=run.id,
            comparison_id=comparison.id,
            policy_id=policy.id,
        )
        with pytest.raises(AgentHubError) as denied:
            await policy_service.create_decision(
                session,
                context=viewer,
                run_id=run.id,
                comparison_id=comparison.id,
                policy_id=policy.id,
            )
        assert denied.value.status_code == 403

        persisted_run = await session.get(EvaluationExperimentRun, run.id)
        assert persisted_run is not None
        persisted_run.purpose = "DEVELOPMENT"
        persisted_run.split = "DEV"
        await session.commit()
        with pytest.raises(AgentHubError) as dev_gate:
            await policy_service.create_decision(
                session,
                context=context,
                run_id=run.id,
                comparison_id=comparison.id,
                policy_id=policy.id,
            )
        assert dev_gate.value.code == "RELEASE_GATE_REQUIRES_HOLDOUT"

        tampered_metrics = dict(comparison.metrics)
        tampered_task_success = dict(tampered_metrics["task_success"])
        tampered_candidate = dict(tampered_task_success["candidate"])
        tampered_candidate["value"] = 0
        tampered_task_success["candidate"] = tampered_candidate
        tampered_metrics["task_success"] = tampered_task_success
        comparison.metrics = tampered_metrics
        await session.commit()

        with pytest.raises(AgentHubError) as create_integrity:
            await policy_service.create_decision(
                session,
                context=context,
                run_id=run.id,
                comparison_id=comparison.id,
                policy_id=policy.id,
            )
        assert create_integrity.value.code == "EVALUATION_COMPARISON_INTEGRITY_ERROR"
        with pytest.raises(AgentHubError) as read_integrity:
            await policy_service.get_decision(
                session,
                context=context,
                run_id=run.id,
                comparison_id=comparison.id,
                policy_id=policy.id,
            )
        assert read_integrity.value.code == "EVALUATION_COMPARISON_INTEGRITY_ERROR"


class _MixedEvidenceDriver(_CountingDriver):
    async def execute(self, session, *, run, variant, item):
        result = await super().execute(session, run=run, variant=variant, item=item)
        if (variant.label, item.case_key) in {("baseline", "gain"), ("candidate", "loss")}:
            result.observation["final_chunk_ids"] = []
        if variant.label == "candidate" and item.case_key == "unknown":
            del result.observation["final_chunk_ids"]
        return result


@pytest.mark.asyncio
async def test_paired_success_real_api_reports_regressions_and_blocks_partial_gate(db_factory):
    settings = Settings(testing=True)
    async with db_factory() as session:
        base = await _seed(session, label=f"paired-{uuid4().hex}")
        run, baseline, candidate, _ = await _release_run(
            session, base, ("gain", "loss", "unknown", "tie")
        )
    await ExperimentRunner(db_factory, driver=_MixedEvidenceDriver()).execute(
        run_id=run.id, owner="paired-worker", settings=settings
    )
    app = create_app(settings)
    app.state.db_session_factory = db_factory
    root = f"/api/v1/workspaces/{base['workspace_id']}/evaluation"
    run_path = f"{root}/experiment-runs/{run.id}"
    async with _client(app, base["user"].id, settings) as client:
        assert (await client.post(run_path + "/metrics")).is_success
        comparison = await client.post(
            run_path + "/comparisons",
            json={
                "baseline_variant_id": str(baseline.id),
                "candidate_variant_id": str(candidate.id),
            },
        )
        assert comparison.is_success, comparison.text
        path = run_path + "/comparisons/" + comparison.json()["id"]
        response = await client.get(path + "/paired-success")
        assert response.is_success, response.text
        report = response.json()
        assert [(r["case_key"], r["delta"]) for r in report["cases"]] == [
            ("gain", 1),
            ("loss", -1),
            ("tie", 0),
            ("unknown", None),
        ]
        assert report["pair_count"] == 3
        assert report["missing_repetitions"] == 1 and report["planned_repetitions"] == 4
        policy = await client.post(
            root + "/release-gate-policies",
            json={
                "name": "paired-coverage",
                "policy_json": {
                    "rules": [
                        {
                            "metric": "success_rate_delta",
                            "rule": "NON_INFERIORITY",
                            "threshold": 0.02,
                        }
                    ]
                },
            },
        )
        assert policy.is_success, policy.text
        decision = await client.post(
            path + "/release-gates", json={"policy_id": policy.json()["id"]}
        )
        assert decision.is_success, decision.text
        artifact = decision.json()
        assert artifact["status"] == "INCONCLUSIVE"
        assert artifact["rule_results"][0]["reason"] == "incomplete_paired_repetitions"
        assert artifact["rule_results"][0]["method_version"] == report["method_version"]
        reread = await client.get(path + "/release-gates/" + policy.json()["id"])
        assert reread.json() == artifact
