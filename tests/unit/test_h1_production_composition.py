from __future__ import annotations

from functools import partial
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from apps import agent_run_composition
from apps.api.agent_runtime_dependencies import get_production_agent_run_service
from apps.worker.tasks import agent_runs
from packages.artifacts.recorder import ToolResultArtifactRecorder
from packages.core.config.settings import Settings
from packages.knowledge.composition import RetrievalComponents
from packages.memory.store import SqlAlchemyMemoryStore
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.tools.audit import SqlAlchemyToolAuditSink
from scripts import portfolio_runtime


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


@pytest.fixture
def runtime_app() -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(testing=True, run_stream_grace_seconds=17, redis_url="memory://")
    app.state.db_session_factory = object()
    return app


def test_api_and_worker_compose_the_same_run_dependencies(monkeypatch, runtime_app) -> None:
    app = runtime_app
    components = RetrievalComponents(
        dense=object(), sparse=object(), reranker=object(), index=object()
    )
    calls: list[Settings] = []

    def fake_components(settings: Settings) -> RetrievalComponents:
        calls.append(settings)
        return components

    monkeypatch.setattr(agent_run_composition, "production_retrieval_components", fake_components)

    api_service = get_production_agent_run_service(_request(app))
    assert get_production_agent_run_service(_request(app)) is api_service
    worker_service = agent_runs._build_service(app.state.settings, app.state.db_session_factory)

    assert calls == [app.state.settings, app.state.settings]
    assert worker_service.stream_registry is not api_service.stream_registry
    assert worker_service.memory_writer.celery_app is agent_runs.celery_app
    assert api_service.memory_writer.celery_app.conf.broker_url == app.state.settings.redis_url
    for service in (api_service, worker_service):
        assert service.stream_grace_seconds == 17
        assert isinstance(service.tool_runtime.audit_sink, SqlAlchemyToolAuditSink)
        assert isinstance(service.thread_context_provider, SqlAlchemyThreadContextProvider)
        assert isinstance(service.artifact_recorder, ToolResultArtifactRecorder)
        assert isinstance(service.memory_selector, SqlAlchemyMemoryStore)
        for identity in ("search_knowledge", "query_wiki"):
            handler = service.tool_runtime.registry.resolve(identity)
            assert isinstance(handler, partial)
            assert handler.keywords["retriever"].components is components


def test_portfolio_provider_reaches_both_run_entry_points(
    monkeypatch, tmp_path, runtime_app
) -> None:
    monkeypatch.setattr(portfolio_runtime, "configure", lambda: None)
    monkeypatch.setattr(portfolio_runtime, "STATE", tmp_path)
    monkeypatch.setattr(agent_run_composition, "production_retrieval_components", lambda _: None)

    with patch.object(agent_run_composition, "AgentRunService"):
        portfolio_runtime.install_provider()
        api = get_production_agent_run_service(_request(runtime_app))
        worker = agent_runs._build_service(
            runtime_app.state.settings, runtime_app.state.db_session_factory
        )
        for service in (api, worker):
            gateway = service.model_gateway_factory(object())
            assert isinstance(gateway.adapter, portfolio_runtime.IncidentProvider)


def test_worker_and_beat_do_not_require_api_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTHUB_AUTH_JWT_SECRET", raising=False)
    for role in ("worker", "beat"):
        settings = Settings(environment="docker", process_role=role, _env_file=None)
        assert settings.auth_jwt_secret

    with pytest.raises(ValueError, match="AUTH_JWT_SECRET"):
        Settings(environment="docker", process_role="api", _env_file=None)


def test_development_environment_aliases_share_cookie_semantics() -> None:
    assert all(
        Settings(environment=name).refresh_cookie_secure is False
        for name in {"local", "test", "testing", "development", "dev"}
    )
