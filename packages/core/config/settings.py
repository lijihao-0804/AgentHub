from __future__ import annotations

from functools import lru_cache

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

    @property
    def database_sync_url(self) -> str:
        if self.database_url.startswith("postgresql+asyncpg://"):
            return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
