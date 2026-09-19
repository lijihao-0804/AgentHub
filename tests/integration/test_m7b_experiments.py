from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentVersion
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.build_identity import StaticBuildIdentityProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.models import (
    EvaluationDatasetVersionStatus,
    EvaluationExperimentRunStatus,
    EvaluationExperimentStatus,
)
from packages.evaluation.service import EvaluationDatasetService
from packages.knowledge.models import (
    Document,
    DocumentRevision,
    KnowledgeBase,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)
from packages.knowledge.snapshots import KnowledgeSnapshotService
from tests.integration.test_m4c_agent_runtime import _seed

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M7-B PostgreSQL integration tests.",
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


def _manager_context(base: dict[str, object], *, viewer: bool = False):
    permissions = {
        "workspace_read",
        "evaluation_read",
        "evaluation_manage",
        "evaluation_run",
        "knowledge_run",
        "agent_edit",
        "agent_run",
        "tool_run",
    }
    if viewer:
        permissions = {"workspace_read", "evaluation_read"}
    return base["context"].model_copy(
        update={
            "workspace_role": "VIEWER" if viewer else "DEVELOPER",
            "permissions": frozenset(permissions),
        }
    )


def _item(case_key: str, *, split: str, ordinal: int) -> dict[str, object]:
    return {
        "case_key": case_key,
        "split": split,
        "category": "RETRIEVAL",
        "input": {"query": f"query-{case_key}"},
        "expected": {"relevant_chunk_ids": [f"chunk-{case_key}"]},
        "tags": ["synthetic"],
        "source_provenance": {"source_kind": "fixture", "source_id": case_key},
        "ordinal": ordinal,
    }


async def _published_version(
    session: AsyncSession,
    base: dict[str, object],
    *,
    items: list[dict[str, object]] | None = None,
):
    context = _manager_context(base)
    service = EvaluationDatasetService()
    dataset = await service.create_dataset(
        session, context=context, name=f"dataset-{uuid4().hex}"
    )
    version = await service.create_version(
        session,
        context=context,
        dataset_id=dataset.id,
        items=items or [_item("dev-1", split="DEV", ordinal=0)],
    )
    version = await service.publish_version(
        session, context=context, dataset_id=dataset.id, version_id=version.id
    )
    return dataset, version


async def _pricing(
    session: AsyncSession,
    base: dict[str, object],
    *,
    model: str = "frozen-model-a",
):
    return await EvaluationDatasetService().create_pricing_snapshot(
        session,
        context=_manager_context(base),
        name="Synthetic pricing",
        provider="fake",
        model=model,
        currency="USD",
        input_price_per_1m=Decimal("0.10"),
        output_price_per_1m=Decimal("0.20"),
        cached_input_price_per_1m=None,
        effective_at=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
        source_note="Synthetic fixture; no provider billing data.",
    )


@pytest.mark.asyncio
async def test_m7b_freezes_experiment_variant_and_queues_immutable_run(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7b-freeze-{uuid4().hex}")
        _, dataset_version = await _published_version(session, base)
        pricing = await _pricing(session, base)
        service = ExperimentService(StaticBuildIdentityProvider("a" * 40))
        context = _manager_context(base)
        agent_version_id = base["version"].id
        pricing_id = pricing.id

        experiment = await service.create_experiment(
            session,
            context=context,
            name="Reproducible development",
            description="M7-B fixture",
            dataset_version_id=dataset_version.id,
            split="DEV",
            purpose="DEVELOPMENT",
            repetitions=2,
        )
        experiment_id = experiment.id
        variant = await service.add_variant(
            session,
            context=context,
            experiment_id=experiment_id,
            label="baseline",
            agent_version_id=agent_version_id,
            pricing_snapshot_id=pricing_id,
            ordinal=0,
            variant_metadata={"temperature_label": "baseline"},
        )
        assert variant.variant_hash
        assert "token" not in str(variant.variant_metadata)

        with pytest.raises(AgentHubError) as duplicate_label:
            await service.add_variant(
                session,
                context=context,
                experiment_id=experiment_id,
                label="baseline",
                agent_version_id=agent_version_id,
                pricing_snapshot_id=pricing_id,
                ordinal=1,
            )
        assert duplicate_label.value.code == "EVALUATION_VARIANT_CONFLICT"
        with pytest.raises(AgentHubError) as duplicate_ordinal:
            await service.add_variant(
                session,
                context=context,
                experiment_id=experiment_id,
                label="second",
                agent_version_id=agent_version_id,
                pricing_snapshot_id=pricing_id,
                ordinal=0,
            )
        assert duplicate_ordinal.value.code == "EVALUATION_VARIANT_CONFLICT"

        ready = await service.finalize_experiment(
            session, context=context, experiment_id=experiment_id
        )
        assert ready.status == EvaluationExperimentStatus.READY
        assert ready.spec_hash
        assert ready.spec_json
        assert "raw_prompt" not in str(ready.spec_json)
        run, exposure = await service.create_run(
            session, context=context, experiment_id=experiment_id
        )
        assert run.status == EvaluationExperimentRunStatus.QUEUED
        assert exposure is None
        assert run.git_commit == "a" * 40
        assert run.experiment_spec_hash == ready.spec_hash
        run_only_context = context.model_copy(update={"permissions": frozenset({"evaluation_run"})})
        run_only, run_only_exposure = await service.create_run(
            session, context=run_only_context, experiment_id=experiment_id
        )
        assert run_only.status == EvaluationExperimentRunStatus.QUEUED
        assert run_only_exposure is None

        with pytest.raises(AgentHubError) as raised:
            await service.add_variant(
                session,
                context=context,
                experiment_id=experiment_id,
                label="late",
                agent_version_id=agent_version_id,
                pricing_snapshot_id=pricing_id,
                ordinal=1,
            )
        assert raised.value.code == "EVALUATION_EXPERIMENT_IMMUTABLE"


@pytest.mark.asyncio
async def test_m7b_requires_published_dataset_and_valid_definition(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7b-definition-{uuid4().hex}")
        context = _manager_context(base)
        dataset_service = EvaluationDatasetService()
        dataset = await dataset_service.create_dataset(
            session, context=context, name="draft dataset"
        )
        draft = await dataset_service.create_version(
            session,
            context=context,
            dataset_id=dataset.id,
            items=[_item("draft", split="DEV", ordinal=0)],
        )
        service = ExperimentService(StaticBuildIdentityProvider("b" * 40))
        with pytest.raises(AgentHubError) as unpublished:
            await service.create_experiment(
                session,
                context=context,
                name="invalid",
                description=None,
                dataset_version_id=draft.id,
                split="DEV",
                purpose="DEVELOPMENT",
            )
        assert unpublished.value.code == "EVALUATION_DATASET_NOT_PUBLISHED"

        published = await dataset_service.publish_version(
            session, context=context, dataset_id=dataset.id, version_id=draft.id
        )
        assert published.status == EvaluationDatasetVersionStatus.PUBLISHED
        with pytest.raises(AgentHubError) as mismatch:
            await service.create_experiment(
                session,
                context=context,
                name="invalid",
                description=None,
                dataset_version_id=published.id,
                split="DEV",
                purpose="HOLDOUT_VALIDATION",
            )
        assert mismatch.value.code == "EVALUATION_EXPERIMENT_INVALID"

        with pytest.raises(AgentHubError) as repetitions:
            await service.create_experiment(
                session,
                context=context,
                name="invalid",
                description=None,
                dataset_version_id=published.id,
                split="DEV",
                purpose="DEVELOPMENT",
                repetitions=6,
            )
        assert repetitions.value.code == "EVALUATION_EXPERIMENT_INVALID"


@pytest.mark.asyncio
async def test_m7b_agent_and_pricing_integrity_are_scoped_and_frozen(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7b-integrity-{uuid4().hex}")
        _, version = await _published_version(session, base)
        good_pricing = await _pricing(session, base)
        bad_pricing = await _pricing(session, base, model="different-model")
        service = ExperimentService(StaticBuildIdentityProvider("c" * 40))
        context = _manager_context(base)
        experiment = await service.create_experiment(
            session,
            context=context,
            name="integrity",
            description=None,
            dataset_version_id=version.id,
            split="DEV",
            purpose="DEVELOPMENT",
        )

        stored_version = await session.get(AgentVersion, base["version"].id)
        assert stored_version is not None
        stored_version.resolved_spec_hash = "b" * 64
        await session.commit()
        with pytest.raises(AgentHubError) as invalid_agent:
            await service.add_variant(
                session,
                context=context,
                experiment_id=experiment.id,
                label="invalid-agent",
                agent_version_id=base["version"].id,
                pricing_snapshot_id=good_pricing.id,
                ordinal=0,
            )
        assert invalid_agent.value.code == "EVALUATION_AGENT_VERSION_INTEGRITY_ERROR"

        stored_version.resolved_spec_hash = canonical_json_hash(stored_version.resolved_spec)
        await session.commit()
        with pytest.raises(AgentHubError) as invalid_pricing:
            await service.add_variant(
                session,
                context=context,
                experiment_id=experiment.id,
                label="invalid-pricing",
                agent_version_id=base["version"].id,
                pricing_snapshot_id=bad_pricing.id,
                ordinal=0,
            )
        assert invalid_pricing.value.code == "EVALUATION_PRICING_MODEL_MISMATCH"


@pytest.mark.asyncio
async def test_m7b_latest_knowledge_is_frozen_at_variant_creation(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7b-latest-{uuid4().hex}")
        knowledge_base = KnowledgeBase(workspace_id=base["workspace_id"], name="Evaluation KB")
        session.add(knowledge_base)
        await session.flush()
        version = await session.get(AgentVersion, base["version"].id)
        assert version is not None
        version.spec_schema_version = 2
        version.resolved_spec = {
            **version.resolved_spec,
            "spec_schema_version": 2,
            "retrieval": {
                "knowledge_binding_mode": "LATEST",
                "knowledge_bindings": [
                    {"knowledge_base_id": str(knowledge_base.id), "binding_mode": "LATEST"}
                ],
            },
        }
        version.resolved_spec_hash = canonical_json_hash(version.resolved_spec)
        await session.commit()

        _, dataset_version = await _published_version(session, base)
        pricing = await _pricing(session, base)
        context = _manager_context(base)
        service = ExperimentService(StaticBuildIdentityProvider("d" * 40))
        experiment = await service.create_experiment(
            session,
            context=context,
            name="latest freeze",
            description=None,
            dataset_version_id=dataset_version.id,
            split="DEV",
            purpose="DEVELOPMENT",
        )
        variant = await service.add_variant(
            session,
            context=context,
            experiment_id=experiment.id,
            label="latest",
            agent_version_id=version.id,
            pricing_snapshot_id=pricing.id,
            ordinal=0,
        )
        frozen = variant.effective_knowledge_snapshots
        assert len(frozen) == 1
        assert frozen[0]["binding_mode"] == "LATEST"
        old_snapshot_id = UUID(frozen[0]["snapshot_id"])

        document = Document(
            workspace_id=base["workspace_id"],
            knowledge_base_id=knowledge_base.id,
            name="new-evaluation-document.txt",
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentRevision(
                workspace_id=base["workspace_id"],
                knowledge_base_id=knowledge_base.id,
                document_id=document.id,
                revision_number=1,
                original_filename="new-evaluation-document.txt",
                blob_key=f"m7b/{uuid4().hex}",
                media_type="text/plain",
                file_size=10,
                ingestion_status=RevisionIngestionStatus.READY,
                lifecycle_status=RevisionLifecycleStatus.ACTIVE,
            )
        )
        await session.flush()
        newer = await KnowledgeSnapshotService().create_current_snapshot(
            session, context, knowledge_base.id
        )
        assert newer.snapshot_id != old_snapshot_id
        assert frozen[0]["snapshot_id"] == str(old_snapshot_id)
        assert frozen[0]["snapshot_content_hash"] != newer.content_hash


@pytest.mark.asyncio
async def test_m7b_holdout_exposures_are_monotonic_and_auditable(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7b-holdout-{uuid4().hex}")
        _, dataset_version = await _published_version(
            session,
            base,
            items=[
                _item("dev", split="DEV", ordinal=0),
                _item("holdout", split="HOLDOUT", ordinal=1),
            ],
        )
        pricing = await _pricing(session, base)
        context = _manager_context(base)
        service = ExperimentService(StaticBuildIdentityProvider("e" * 40))
        experiment = await service.create_experiment(
            session,
            context=context,
            name="holdout",
            description=None,
            dataset_version_id=dataset_version.id,
            split="HOLDOUT",
            purpose="HOLDOUT_VALIDATION",
        )
        await service.add_variant(
            session,
            context=context,
            experiment_id=experiment.id,
            label="candidate",
            agent_version_id=base["version"].id,
            pricing_snapshot_id=pricing.id,
            ordinal=0,
        )
        await service.finalize_experiment(session, context=context, experiment_id=experiment.id)
        first, first_exposure = await service.create_run(
            session, context=context, experiment_id=experiment.id
        )
        second, second_exposure = await service.create_run(
            session, context=context, experiment_id=experiment.id
        )
        assert first.status == second.status == EvaluationExperimentRunStatus.QUEUED
        assert (first_exposure, second_exposure) == (1, 2)
        assert await service.holdout_exposure_count(
            session, context=context, experiment_id=experiment.id
        ) == 2


@pytest.mark.asyncio
async def test_m7b_workspace_scoping_and_viewer_read(db_factory) -> None:
    async with db_factory() as session:
        owner = await _seed(session, label=f"m7b-owner-{uuid4().hex}")
        other = await _seed(session, label=f"m7b-other-{uuid4().hex}")
        _, version = await _published_version(session, owner)
        pricing = await _pricing(session, owner)
        service = ExperimentService(StaticBuildIdentityProvider("f" * 40))
        owner_context = _manager_context(owner)
        experiment = await service.create_experiment(
            session,
            context=owner_context,
            name="scoped",
            description=None,
            dataset_version_id=version.id,
            split="DEV",
            purpose="DEVELOPMENT",
        )
        viewer = _manager_context(owner, viewer=True)
        assert await service.get_experiment(
            session, context=viewer, experiment_id=experiment.id
        )
        with pytest.raises(AgentHubError) as forbidden:
            await service.create_experiment(
                session,
                context=viewer,
                name="forbidden",
                description=None,
                dataset_version_id=version.id,
                split="DEV",
                purpose="DEVELOPMENT",
            )
        assert forbidden.value.code == "FORBIDDEN"
        with pytest.raises(AgentHubError) as isolated:
            await service.get_experiment(
                session,
                context=_manager_context(other),
                experiment_id=experiment.id,
            )
        assert isolated.value.code == "EVALUATION_EXPERIMENT_NOT_FOUND"
        assert pricing.workspace_id == owner["workspace_id"]
