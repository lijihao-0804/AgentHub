from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from apps.api.routes import evaluation as evaluation_routes
from packages.control_plane.rbac import EVALUATION_MANAGE, EVALUATION_READ


class _Session:
    def __init__(self, *, workspace_id, version_id, dataset_id) -> None:
        self.workspace_id = workspace_id
        self.version_id = version_id
        self.dataset_id = dataset_id
        self.execute_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        return [(self.workspace_id, self.version_id, self.dataset_id)]


class _ExperimentService:
    def __init__(self, experiment: object) -> None:
        self.experiment = experiment
        self.create_kwargs = None

    async def list_experiments(self, session, *, context, limit, offset):
        del session, context, limit, offset
        return [self.experiment], 1

    async def create_experiment(self, session, *, context, **kwargs):
        del session, context
        self.create_kwargs = kwargs
        return self.experiment

    async def finalize_experiment(self, session, *, context, experiment_id):
        del session, context, experiment_id
        return self.experiment

    async def list_variants(self, session, *, context, experiment_id):
        del session, context, experiment_id
        return []

    async def holdout_exposure_count(self, session, *, context, experiment_id):
        del session, context, experiment_id
        return None


def _experiment(*, workspace_id, version_id):
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        name="Viewer-visible experiment",
        description=None,
        dataset_version_id=version_id,
        dataset_content_hash="a" * 64,
        dataset_schema_version=2,
        split="DEV",
        purpose="DEVELOPMENT",
        evidence_level="DETERMINISTIC_CONFORMANCE",
        repetitions=1,
        status="DRAFT",
        build_sha="b" * 40,
        evaluator_manifest={},
        spec_json=None,
        spec_hash=None,
        holdout_exposure_index=None,
        created_by=uuid4(),
        created_at=datetime.now(UTC),
    )


def _app(*, session, context):
    app = FastAPI()
    app.include_router(evaluation_routes.router)

    async def override_db() -> AsyncIterator[_Session]:
        yield session

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_workspace_context] = lambda: context
    return app


@pytest.mark.asyncio
async def test_experiment_list_projects_dataset_id_for_evaluation_reader(monkeypatch) -> None:
    workspace_id = uuid4()
    version_id = uuid4()
    dataset_id = uuid4()
    experiment = _experiment(workspace_id=workspace_id, version_id=version_id)
    session = _Session(workspace_id=workspace_id, version_id=version_id, dataset_id=dataset_id)
    service = _ExperimentService(experiment)
    context = SimpleNamespace(permissions=frozenset({EVALUATION_READ}))
    monkeypatch.setattr(evaluation_routes, "ExperimentService", lambda: service)
    app = _app(session=session, context=context)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/workspaces/{workspace_id}/evaluation/experiments?limit=1"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["dataset_id"] == str(dataset_id)
    assert body["items"][0]["dataset_version_id"] == str(version_id)
    assert session.execute_count == 1


@pytest.mark.asyncio
async def test_create_experiment_response_projects_dataset_id(monkeypatch) -> None:
    workspace_id = uuid4()
    version_id = uuid4()
    dataset_id = uuid4()
    experiment = _experiment(workspace_id=workspace_id, version_id=version_id)
    session = _Session(workspace_id=workspace_id, version_id=version_id, dataset_id=dataset_id)
    service = _ExperimentService(experiment)
    context = SimpleNamespace(permissions=frozenset({EVALUATION_MANAGE}))
    monkeypatch.setattr(evaluation_routes, "ExperimentService", lambda: service)
    app = _app(session=session, context=context)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/workspaces/{workspace_id}/evaluation/experiments",
            json={
                "name": "Stub experiment",
                "description": None,
                "dataset_version_id": str(version_id),
                "split": "DEV",
                "purpose": "DEVELOPMENT",
                "repetitions": 1,
                "judge_model_profile_id": None,
            },
        )

    assert response.status_code == 201
    assert response.json()["dataset_id"] == str(dataset_id)
    assert service.create_kwargs["dataset_version_id"] == version_id
    assert session.execute_count == 1


@pytest.mark.asyncio
async def test_finalize_experiment_response_projects_dataset_id(monkeypatch) -> None:
    workspace_id = uuid4()
    version_id = uuid4()
    dataset_id = uuid4()
    experiment = _experiment(workspace_id=workspace_id, version_id=version_id)
    session = _Session(workspace_id=workspace_id, version_id=version_id, dataset_id=dataset_id)
    service = _ExperimentService(experiment)
    context = SimpleNamespace(permissions=frozenset({EVALUATION_MANAGE}))
    monkeypatch.setattr(evaluation_routes, "ExperimentService", lambda: service)
    app = _app(session=session, context=context)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/workspaces/{workspace_id}/evaluation/experiments/{experiment.id}/finalize"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["dataset_id"] == str(dataset_id)
    assert body["variants"] == []
    assert session.execute_count == 1
