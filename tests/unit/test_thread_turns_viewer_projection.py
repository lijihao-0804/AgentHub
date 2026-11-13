from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.knowledge_dependencies import get_agent_run_workspace_context
from apps.api.routes import threads as thread_routes


class _TurnService:
    def __init__(self, *, turn: object, run: object) -> None:
        self._rows = [(turn, run)]

    async def turn_summaries(self, context: object, thread_id: object):
        del context, thread_id
        return self._rows


def _client(monkeypatch, *, context: object, turn: object, run: object) -> TestClient:
    service = _TurnService(turn=turn, run=run)
    monkeypatch.setattr(thread_routes, "_service", lambda request: service)

    app = FastAPI()
    app.include_router(thread_routes.router)
    app.dependency_overrides[get_agent_run_workspace_context] = lambda: context
    return TestClient(app)


def test_viewer_gets_redacted_turn_metadata_instead_of_internal_error(monkeypatch) -> None:
    workspace_id = uuid4()
    thread_id = uuid4()
    turn = SimpleNamespace(
        id=uuid4(),
        thread_id=thread_id,
        sequence=1,
        user_input="sensitive prompt",
        agent_run_id=uuid4(),
        created_at=datetime.now(UTC),
    )
    run = SimpleNamespace(status="SUCCEEDED", final_output="sensitive answer", failure_code=None)
    client = _client(
        monkeypatch,
        context=SimpleNamespace(permissions=frozenset({"workspace_read"})),
        turn=turn,
        run=run,
    )

    response = client.get(f"/api/v1/workspaces/{workspace_id}/threads/{thread_id}/turns")

    assert response.status_code == 200
    [item] = response.json()
    assert item["id"] == str(turn.id)
    assert item["agent_run_id"] == str(turn.agent_run_id)
    assert item["status"] == "SUCCEEDED"
    assert item["user_input"] is None
    assert item["final_output"] is None
    assert "sensitive" not in response.text


def test_agent_run_permission_still_reads_turn_content(monkeypatch) -> None:
    workspace_id = uuid4()
    thread_id = uuid4()
    turn = SimpleNamespace(
        id=uuid4(),
        thread_id=thread_id,
        sequence=1,
        user_input="allowed prompt",
        agent_run_id=uuid4(),
        created_at=datetime.now(UTC),
    )
    run = SimpleNamespace(status="SUCCEEDED", final_output="allowed answer", failure_code=None)
    client = _client(
        monkeypatch,
        context=SimpleNamespace(permissions=frozenset({"workspace_read", "agent_run"})),
        turn=turn,
        run=run,
    )

    response = client.get(f"/api/v1/workspaces/{workspace_id}/threads/{thread_id}/turns")

    assert response.status_code == 200
    [item] = response.json()
    assert item["user_input"] == "allowed prompt"
    assert item["final_output"] == "allowed answer"
