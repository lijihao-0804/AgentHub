from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.service import EvaluationDatasetService
from packages.evaluation.validation import dataset_content_hash
from tests.integration.test_m4c_agent_runtime import _seed

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run Enhancement 2 PostgreSQL integration tests.",
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


def _manager_context(base: dict[str, object]):
    return base["context"].model_copy(
        update={
            "permissions": frozenset({"workspace_read", "evaluation_manage"}),
        }
    )


def _viewer_context(base: dict[str, object]):
    return base["context"].model_copy(
        update={"workspace_role": "VIEWER", "permissions": frozenset({"workspace_read"})}
    )


def _base_item(case_key: str, ordinal: int) -> dict[str, object]:
    return {
        "case_key": case_key,
        "split": "DEV",
        "category": "RETRIEVAL",
        "input": {"query": f"base query {case_key}"},
        "expected": {"relevant_chunk_ids": [f"chunk-{case_key}"]},
        "tags": ["fixture"],
        "source_provenance": {"source_kind": "fixture", "source_id": case_key},
        "ordinal": ordinal,
    }


async def _create_run(
    session: AsyncSession,
    base: dict[str, object],
    *,
    input_text: str = "Observed production request",
    status: str = "FAILED",
    failure_code: str | None = "MODEL_TIMEOUT",
) -> AgentRun:
    run = AgentRun(
        workspace_id=base["workspace_id"],
        agent_version_id=base["version"].id,
        input_text=input_text,
        status=status,
        failure_code=failure_code,
        resolved_spec_hash="r" * 64,
        created_by=base["user"].id,
    )
    session.add(run)
    await session.commit()
    return run


@pytest.mark.asyncio
async def test_imports_run_as_new_draft_without_overwriting_expected_or_base(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"enhancement2-import-{uuid4().hex}")
        context = _manager_context(base)
        service = EvaluationDatasetService()
        dataset = await service.create_dataset(
            session, context=context, name=f"Regression dataset {uuid4().hex}"
        )
        base_version = await service.create_version(
            session,
            context=context,
            dataset_id=dataset.id,
            items=[_base_item("base-1", 0), _base_item("base-2", 1)],
        )
        base_hash = base_version.content_hash
        run = await _create_run(session, base, input_text="Payment timed out")

        imported = await service.create_version_from_run(
            session,
            context=context,
            dataset_id=dataset.id,
            base_version_id=base_version.id,
            run_id=run.id,
            case_key="production-payment-timeout",
            split="HOLDOUT",
            category="FAILURE",
            expected={"status": "SUCCEEDED", "failure_code": None},
            tags=["production", "regression"],
        )
        items = await service.list_version_items(
            session,
            context=context,
            dataset_id=dataset.id,
            version_id=imported.id,
        )
        base_items = await service.list_version_items(
            session,
            context=context,
            dataset_id=dataset.id,
            version_id=base_version.id,
        )

        assert imported.version_number == 2
        assert imported.status == "DRAFT"
        assert len(items) == 3
        assert base_version.content_hash == base_hash
        assert len(base_items) == 2
        imported_item = next(
            item for item in items if item.case_key == "production-payment-timeout"
        )
        assert imported_item.input == {"scenario": "Payment timed out"}
        assert imported_item.expected == {"status": "SUCCEEDED", "failure_code": None}
        assert imported_item.source_provenance == {
            "source_kind": "agent_run",
            "source_id": str(run.id),
            "agent_version_id": str(run.agent_version_id),
            "resolved_spec_hash": "r" * 64,
            "observed_status": "FAILED",
            "observed_failure_code": "MODEL_TIMEOUT",
        }
        assert imported_item.ordinal == 2
        assert imported.content_hash == dataset_content_hash(
            [
                {
                    "case_key": item.case_key,
                    "split": item.split,
                    "category": item.category,
                    "input": item.input,
                    "expected": item.expected,
                    "tags": item.tags,
                    "source_provenance": item.source_provenance,
                    "ordinal": item.ordinal,
                }
                for item in items
            ],
            schema_version=base_version.schema_version,
        )


@pytest.mark.asyncio
async def test_import_enforces_permissions_workspace_scope_and_source_deduplication(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        owner = await _seed(session, label=f"enhancement2-owner-{uuid4().hex}")
        other = await _seed(session, label=f"enhancement2-other-{uuid4().hex}")
        service = EvaluationDatasetService()
        owner_context = _manager_context(owner)
        dataset = await service.create_dataset(
            session, context=owner_context, name=f"Owner dataset {uuid4().hex}"
        )
        base_version = await service.create_version(
            session,
            context=owner_context,
            dataset_id=dataset.id,
            items=[_base_item("base-only", 0)],
        )
        owner_run = await _create_run(session, owner, input_text="owner request")
        other_run = await _create_run(session, other, input_text="other request")

        with pytest.raises(AgentHubError) as viewer_error:
            await service.create_version_from_run(
                session,
                context=_viewer_context(owner),
                dataset_id=dataset.id,
                base_version_id=base_version.id,
                run_id=owner_run.id,
                case_key="viewer-case",
                split="DEV",
                category="FAILURE",
                expected={"status": "FAILED", "failure_code": "MODEL_TIMEOUT"},
                tags=[],
            )
        assert viewer_error.value.code == "FORBIDDEN"

        with pytest.raises(AgentHubError) as cross_workspace:
            await service.create_version_from_run(
                session,
                context=owner_context,
                dataset_id=dataset.id,
                base_version_id=base_version.id,
                run_id=other_run.id,
                case_key="cross-workspace-run",
                split="DEV",
                category="FAILURE",
                expected={"status": "FAILED", "failure_code": "MODEL_TIMEOUT"},
                tags=[],
            )
        assert cross_workspace.value.code == "AGENT_RUN_NOT_FOUND"

        first = await service.create_version_from_run(
            session,
            context=owner_context,
            dataset_id=dataset.id,
            base_version_id=base_version.id,
            run_id=owner_run.id,
            case_key="owner-case",
            split="DEV",
            category="FAILURE",
            expected={"status": "FAILED", "failure_code": "MODEL_TIMEOUT"},
            tags=[],
        )
        with pytest.raises(AgentHubError) as duplicate:
            await service.create_version_from_run(
                session,
                context=owner_context,
                dataset_id=dataset.id,
                base_version_id=first.id,
                run_id=owner_run.id,
                case_key="owner-case-again",
                split="DEV",
                category="FAILURE",
                expected={"status": "FAILED", "failure_code": "MODEL_TIMEOUT"},
                tags=[],
            )
        assert first.status == "DRAFT"
        assert duplicate.value.code == "EVALUATION_DATASET_SOURCE_DUPLICATE"

        with pytest.raises(AgentHubError) as missing_run:
            await service.create_version_from_run(
                session,
                context=owner_context,
                dataset_id=dataset.id,
                base_version_id=base_version.id,
                run_id=uuid4(),
                case_key="missing-run",
                split="DEV",
                category="FAILURE",
                expected={"status": "FAILED", "failure_code": "MODEL_TIMEOUT"},
                tags=[],
            )
        assert missing_run.value.code == "AGENT_RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_unknown_category_still_uses_existing_dataset_validation(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"enhancement2-validation-{uuid4().hex}")
        context = _manager_context(base)
        service = EvaluationDatasetService()
        dataset = await service.create_dataset(session, context=context, name="Validation dataset")
        base_version = await service.create_version(
            session,
            context=context,
            dataset_id=dataset.id,
            items=[_base_item("base", 0)],
        )
        run = await _create_run(session, base, input_text="unknown category")

        with pytest.raises(AgentHubError) as invalid:
            await service.create_version_from_run(
                session,
                context=context,
                dataset_id=dataset.id,
                base_version_id=base_version.id,
                run_id=run.id,
                case_key="invalid-category",
                split="DEV",
                category="NOT_A_CATEGORY",
                expected={"status": "FAILED", "failure_code": "MODEL_TIMEOUT"},
                tags=[],
            )
        assert invalid.value.code == "EVALUATION_DATASET_INVALID"
