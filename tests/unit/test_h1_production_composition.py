from __future__ import annotations

from functools import partial

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from apps.api.agent_runtime_dependencies import get_production_agent_run_service
from packages.core.config.settings import Settings
from packages.knowledge.composition import RetrievalComponents
from packages.tools.audit import SqlAlchemyToolAuditSink


def _request(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
            "scheme": "http",
        }
    )


def test_production_agent_run_composition_reuses_real_component_graph(monkeypatch) -> None:
    app = FastAPI()
    app.state.settings = Settings(testing=True)
    app.state.db_session_factory = object()
    components = RetrievalComponents(
        dense=object(), sparse=object(), reranker=object(), index=object()
    )
    calls: list[Settings] = []

    def fake_components(settings: Settings) -> RetrievalComponents:
        calls.append(settings)
        return components

    monkeypatch.setattr(
        "apps.api.agent_runtime_dependencies.production_retrieval_components",
        fake_components,
    )

    first = get_production_agent_run_service(_request(app))
    second = get_production_agent_run_service(_request(app))

    assert first is second
    assert calls == [app.state.settings]
    assert isinstance(first.tool_runtime.audit_sink, SqlAlchemyToolAuditSink)
    search_handler = first.tool_runtime.registry._handlers["search_knowledge"]
    assert isinstance(search_handler, partial)
    assert search_handler.keywords["retriever"].components is components


def test_worker_and_beat_do_not_require_api_jwt_secret() -> None:
    for role in ("worker", "beat"):
        settings = Settings(environment="docker", process_role=role)
        assert settings.auth_jwt_secret

    with pytest.raises(ValueError, match="AUTH_JWT_SECRET"):
        Settings(environment="docker", process_role="api")


def test_development_environment_aliases_share_cookie_semantics() -> None:
    assert all(
        Settings(environment=name).refresh_cookie_secure is False
        for name in {"local", "test", "testing", "development", "dev"}
    )
