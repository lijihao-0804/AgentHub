from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.service import EvaluationDatasetService
from tests.integration.test_m4c_agent_runtime import _seed

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M7-A PostgreSQL integration tests.",
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
            "permissions": frozenset(
                {"workspace_read", "evaluation_manage"}
            )
        }
    )


def _viewer_context(base: dict[str, object]):
    return base["context"].model_copy(
        update={"workspace_role": "VIEWER", "permissions": frozenset({"workspace_read"})}
    )


def _item(case_key: str = "case-1", ordinal: int = 0) -> dict[str, object]:
    return {
        "case_key": case_key,
        "split": "DEV",
        "category": "RETRIEVAL",
        "input": {"query": f"query-{case_key}"},
        "expected": {"relevant_chunk_ids": [f"chunk-{case_key}"]},
        "tags": ["synthetic"],
        "source_provenance": {"source_kind": "fixture", "source_id": case_key},
        "ordinal": ordinal,
    }


@pytest.mark.asyncio
async def test_m7a_dataset_version_publish_and_immutability(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7a-publish-{uuid4().hex}")
        service = EvaluationDatasetService()
        context = _manager_context(base)
        dataset = await service.create_dataset(
            session,
            context=context,
            name="Synthetic QA",
            description="M7-A fixture",
        )
        first = await service.create_version(
            session,
            context=context,
            dataset_id=dataset.id,
            items=[_item()],
        )
        second = await service.create_version(
            session,
            context=context,
            dataset_id=dataset.id,
            items=[_item("case-2")],
        )

        assert first.version_number == 1
        assert second.version_number == 2
        published = await service.publish_version(
            session,
            context=context,
            dataset_id=dataset.id,
            version_id=first.id,
        )
        assert published.status == "PUBLISHED"
        assert len(await service.list_version_items(
            session,
            context=context,
            dataset_id=dataset.id,
            version_id=first.id,
        )) == 1

        with pytest.raises(AgentHubError) as raised:
            await service.publish_version(
                session,
                context=context,
                dataset_id=dataset.id,
                version_id=first.id,
            )
        assert raised.value.code == "EVALUATION_DATASET_IMMUTABLE"


@pytest.mark.asyncio
async def test_m7a_workspace_isolation_and_viewer_read(db_factory) -> None:
    async with db_factory() as session:
        owner = await _seed(session, label=f"m7a-owner-{uuid4().hex}")
        other = await _seed(session, label=f"m7a-other-{uuid4().hex}")
        service = EvaluationDatasetService()
        dataset = await service.create_dataset(
            session,
            context=_manager_context(owner),
            name="Owner dataset",
        )
        await service.create_version(
            session,
            context=_manager_context(owner),
            dataset_id=dataset.id,
            items=[_item()],
        )

        assert await service.list_datasets(
            session, context=_viewer_context(owner)
        )
        with pytest.raises(AgentHubError) as raised:
            await service.get_dataset(
                session,
                context=_viewer_context(other),
                dataset_id=dataset.id,
            )
        assert raised.value.code == "RESOURCE_NOT_FOUND"
        with pytest.raises(AgentHubError) as forbidden:
            await service.create_dataset(
                session,
                context=_viewer_context(owner),
                name="not allowed",
            )
        assert forbidden.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_m7a_pricing_snapshot_uses_decimal_and_stable_hash(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7a-pricing-{uuid4().hex}")
        service = EvaluationDatasetService()
        context = _manager_context(base)
        effective_at = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
        first = await service.create_pricing_snapshot(
            session,
            context=context,
            name="Synthetic pricing",
            provider="fake",
            model="fake-model",
            currency="usd",
            input_price_per_1m=Decimal("0.12345678"),
            output_price_per_1m="0.87654321",
            cached_input_price_per_1m=None,
            effective_at=effective_at,
            source_note="Synthetic fixture; no provider billing data.",
        )
        second = await service.create_pricing_snapshot(
            session,
            context=context,
            name="Synthetic pricing",
            provider="fake",
            model="fake-model",
            currency="USD",
            input_price_per_1m="0.12345678",
            output_price_per_1m=Decimal("0.87654321"),
            cached_input_price_per_1m=None,
            effective_at=effective_at,
            source_note="Synthetic fixture; no provider billing data.",
        )

        assert first.currency == "USD"
        assert first.input_price_per_1m == Decimal("0.12345678")
        assert first.content_hash == second.content_hash
        assert len(await service.list_pricing_snapshots(session, context=context)) == 2
        with pytest.raises(AgentHubError) as raised:
            await service.create_pricing_snapshot(
                session,
                context=context,
                name="bad",
                provider="fake",
                model="fake-model",
                currency="USD",
                input_price_per_1m=0.1,
                output_price_per_1m=Decimal("0"),
                cached_input_price_per_1m=None,
                effective_at=effective_at,
                source_note="test",
            )
        assert raised.value.code == "EVALUATION_PRICING_INVALID"
