from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis import asyncio as redis_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.api.routes.auth import router as auth_router
from apps.api.routes.tenancy import router as tenancy_router
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.errors.handlers import install_error_handlers
from packages.core.http.request_id import RequestIdMiddleware
from packages.core.logging.json_logging import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    app.state.db_engine = None
    app.state.db_session_factory = None
    app.state.redis = None

    if not settings.testing:
        app.state.db_engine, app.state.db_session_factory = create_database(settings.database_url)
        app.state.redis = redis_asyncio.from_url(settings.redis_url, decode_responses=True)

    try:
        yield
    finally:
        if app.state.db_engine is not None:
            await app.state.db_engine.dispose()
        if app.state.redis is not None:
            await app.state.redis.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    app = FastAPI(
        title="AgentHub API",
        version="0.1.0",
        description="Enterprise Agent Runtime & Control Plane",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.db_engine = None
    app.state.db_session_factory = None
    app.state.redis = None
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(tenancy_router)

    @app.get("/api/v1/health", tags=["system"])
    async def health(request: Request) -> dict[str, str]:
        return {"status": "ok", "service": "agenthub-api", "request_id": request.state.request_id}

    @app.get("/api/v1/ready", tags=["system"])
    async def ready(request: Request):
        postgres = await probe_postgres(request.app, app_settings)
        payload = {
            "status": "ready" if postgres == "healthy" else "not_ready",
            "dependencies": {"postgres": postgres},
            "request_id": request.state.request_id,
        }
        if postgres != "healthy":
            return JSONResponse(status_code=503, content=payload)
        return payload

    @app.get("/api/v1/dependencies", tags=["system"])
    async def dependencies(request: Request) -> dict[str, Any]:
        statuses = await probe_dependencies(request.app, app_settings)
        return {"dependencies": statuses, "request_id": request.state.request_id}

    return app


async def probe_postgres(app: FastAPI, settings: Settings) -> str:
    engine: AsyncEngine | None = app.state.db_engine
    if engine is None:
        if settings.testing:
            return "degraded"
        return "degraded"
    try:
        async with asyncio.timeout(settings.ready_timeout_ms / 1000):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        return "healthy"
    except Exception:
        logger.warning("dependency_probe_failed", extra={"dependency": "postgres"}, exc_info=True)
        return "degraded"


async def probe_redis(app: FastAPI, settings: Settings) -> str:
    client = app.state.redis
    if client is None:
        return "disabled" if settings.testing else "degraded"
    try:
        async with asyncio.timeout(settings.ready_timeout_ms / 1000):
            await client.ping()
        return "healthy"
    except Exception:
        logger.warning("dependency_probe_failed", extra={"dependency": "redis"}, exc_info=True)
        return "degraded"


async def probe_qdrant(settings: Settings) -> str:
    try:
        async with httpx.AsyncClient(timeout=settings.ready_timeout_ms / 1000) as client:
            response = await client.get(f"{settings.qdrant_url.rstrip('/')}/healthz")
            response.raise_for_status()
        return "healthy"
    except Exception:
        return "degraded"


async def probe_dependencies(app: FastAPI, settings: Settings) -> dict[str, str]:
    postgres, redis, qdrant = await asyncio.gather(
        probe_postgres(app, settings),
        probe_redis(app, settings),
        probe_qdrant(settings),
    )
    return {
        "postgres": postgres,
        "redis": redis,
        "qdrant": qdrant,
        "langfuse": "disabled" if not settings.langfuse_enabled else "degraded",
    }
