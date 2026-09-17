from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTHUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "agenthub-api"
    environment: str = "local"
    log_level: str = "INFO"
    testing: bool = False
    database_url: str = "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    langfuse_enabled: bool = False
    request_id_header: str = "X-Request-ID"
    ready_timeout_ms: int = Field(default=500, ge=50, le=10_000)
    auth_jwt_secret: str = Field(default="local-dev-only-change-me-32-characters", min_length=32)
    auth_access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    auth_refresh_token_ttl_seconds: int = Field(default=2_592_000, ge=300, le=31_536_000)
    auth_refresh_cookie_name: str = "agenthub_refresh"
    auth_refresh_cookie_path: str = "/api/v1/auth"
    auth_refresh_cookie_samesite: Literal["lax", "strict"] = "lax"
    auth_cookie_secure: bool | None = None

    @property
    def database_sync_url(self) -> str:
        if self.database_url.startswith("postgresql+asyncpg://"):
            return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        return self.database_url

    @property
    def refresh_cookie_secure(self) -> bool:
        if self.auth_cookie_secure is not None:
            return self.auth_cookie_secure
        return self.environment.lower() not in {"local", "test", "development"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
