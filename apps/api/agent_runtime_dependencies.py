"""Production composition for AgentRun execution dependencies."""

from __future__ import annotations

from fastapi import FastAPI, Request

from apps.agent_run_composition import build_agent_run_service
from packages.agent_runtime.runtime import AgentRunService
from packages.core.errors.exceptions import AgentHubError
from packages.memory.queue import CeleryMemoryWriteQueue


def get_production_agent_run_service(request: Request) -> AgentRunService:
    app: FastAPI = request.app
    factory = getattr(app.state, "db_session_factory", None)
    if factory is None:
        raise AgentHubError("DATABASE_NOT_CONFIGURED", "Database access is not configured.", 503)
    service = getattr(app.state, "agent_run_service", None)
    if service is None:
        from apps.worker.celery_app import create_celery_app

        settings = app.state.settings
        service = build_agent_run_service(
            settings,
            factory,
            memory_writer=CeleryMemoryWriteQueue(create_celery_app(settings)),
        )
        app.state.agent_run_service = service
    return service


__all__ = ["get_production_agent_run_service"]
