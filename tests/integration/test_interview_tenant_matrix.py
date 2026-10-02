"""Cross-tenant API conformance over real persisted rows; not prompt-injection ASR."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from packages.agent_runtime.models import AgentRun
from packages.core.auth.security import issue_access_token
from packages.core.config.settings import Settings
from packages.feedback.models import RunFeedback
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database

__all__ = ["db_factory", "migrated_database"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="PostgreSQL required"
    ),
]


@pytest.mark.asyncio
async def test_two_tenant_read_write_matrix_and_healthy_control(db_factory):
    async with db_factory() as session:
        a = await _seed(session, label=f"tenant-a-{uuid4().hex}")
        b = await _seed(session, label=f"tenant-b-{uuid4().hex}")
        runs = []
        for fixture in (a, b):
            row = AgentRun(
                workspace_id=fixture["workspace_id"],
                agent_version_id=fixture["version"].id,
                created_by=fixture["user"].id,
                input_text="Tenant-private test input",
                status="SUCCEEDED",
                final_output="TenantBPrivate" if fixture is b else "control",
            )
            session.add(row)
            runs.append(row)
        await session.commit()
    settings = Settings(testing=True)
    app = create_app(settings)
    app.state.db_session_factory = db_factory

    async def db():
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    a_path = f"/api/v1/workspaces/{a['workspace_id']}"
    b_path = f"/api/v1/workspaces/{b['workspace_id']}"
    trials = [
        ("read", "GET", b_path, None),
        ("read", "GET", f"{b_path}/runs/{runs[1].id}", None),
        ("read", "GET", f"{b_path}/runs/{runs[1].id}/tools", None),
        ("read", "GET", f"{a_path}/runs/{runs[1].id}/feedback", None),
        (
            "write",
            "POST",
            f"{b_path}/runs/{runs[1].id}/feedback",
            {"client_key": "foreign", "rating": 1, "category": "OTHER"},
        ),
        (
            "write",
            "POST",
            f"{a_path}/runs/{runs[1].id}/feedback",
            {"client_key": "mis-scoped", "rating": 1, "category": "OTHER"},
        ),
    ]
    records = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {issue_access_token(a['user'].id, settings)}"},
    ) as client:
        for kind, method, path, body in trials:
            response = await client.request(method, path, json=body)
            assert response.status_code in {403, 404}, response.text
            assert "TenantBPrivate" not in response.text
            records.append(
                {
                    "kind": kind,
                    "method": method,
                    "status_code": response.status_code,
                    "error_code": response.json()["error"]["code"],
                    "content_leak": False,
                }
            )
        healthy = await client.post(
            f"{a_path}/runs/{runs[0].id}/feedback",
            json={"client_key": "control", "rating": 1, "category": "OTHER"},
        )
        assert healthy.status_code == 201, healthy.text
    async with db_factory() as session:
        foreign_writes = await session.scalar(
            select(func.count())
            .select_from(RunFeedback)
            .where(RunFeedback.workspace_id == b["workspace_id"])
        )
        own_writes = await session.scalar(
            select(func.count())
            .select_from(RunFeedback)
            .where(RunFeedback.workspace_id == a["workspace_id"])
        )
        assert foreign_writes == 0 and own_writes == 1
    report = {
        "schema_version": 1,
        "driver_kind": "controlled",
        "source": "real_api_jwt_real_postgres",
        "records": records,
        "read_leaks": {"numerator": 0, "denominator": 4},
        "unauthorized_writes": {"numerator": foreign_writes, "denominator": 2},
        "healthy_control_created_count": own_writes,
        "limitation": "Six API tenant scenarios; not every resource or real model attack.",
    }
    if destination := os.environ.get("AGENTHUB_TENANT_MATRIX_OUTPUT"):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
