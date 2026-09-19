"""Framework-owned LangGraph PostgreSQL checkpoint boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def configure_windows_asyncio_policy() -> None:
    """Configure psycopg's selector policy at an explicit runtime boundary."""

    policy_type = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_type is None or isinstance(asyncio.get_event_loop_policy(), policy_type):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop_policy(policy_type())


class CheckpointProbe(Protocol):
    async def has_checkpoint(self, workspace_id: UUID | str, run_id: UUID | str) -> bool: ...


def checkpoint_thread_id(workspace_id: UUID | str, run_id: UUID | str) -> str:
    """Stable tenant-safe identity used by every graph invocation for a Run."""

    return f"agenthub:{workspace_id}:{run_id}"


def checkpoint_config(workspace_id: UUID | str, run_id: UUID | str) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": checkpoint_thread_id(workspace_id, run_id),
            # LangGraph's default graph namespace is the empty namespace; the
            # tenant/run-scoped thread_id is the durable identity we own.
            "checkpoint_ns": "",
        }
    }


def checkpoint_dsn(database_url: str) -> str:
    """Route framework tables to the schema owned by the bootstrap command."""

    sync_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    sync_url = sync_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parts = urlsplit(sync_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = "-csearch_path=langgraph_checkpoint,public"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class LangGraphCheckpointAdapter:
    """Opens short-lived framework checkpointer scopes; never bootstraps on startup."""

    def __init__(self, database_url: str) -> None:
        configure_windows_asyncio_policy()
        self.database_url = database_url

    @asynccontextmanager
    async def checkpointer(self) -> AsyncIterator[AsyncPostgresSaver]:
        async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(self.database_url)) as saver:
            yield saver

    @staticmethod
    def config_for_run(workspace_id: UUID | str, run_id: UUID | str) -> dict[str, Any]:
        return checkpoint_config(workspace_id, run_id)

    @staticmethod
    def safe_state(state: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a checkpoint projection contains JSON-like values only."""

        result: dict[str, Any] = {}
        for key, value in state.items():
            if not isinstance(key, str):
                raise TypeError("checkpoint state keys must be strings")
            result[key] = _safe_value(value)
        return result


class PostgresCheckpointProbe:
    """Provider-neutral checkpoint existence probe for reconciliation."""

    def __init__(self, adapter: LangGraphCheckpointAdapter) -> None:
        self.adapter = adapter

    async def has_checkpoint(self, workspace_id: UUID | str, run_id: UUID | str) -> bool:
        async with self.adapter.checkpointer() as checkpointer:
            checkpoint = await checkpointer.aget_tuple(
                self.adapter.config_for_run(workspace_id, run_id)
            )
            return checkpoint is not None


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _safe_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(child) for child in value]
    raise TypeError(f"unsupported checkpoint state value: {type(value).__name__}")


__all__ = [
    "CheckpointProbe",
    "LangGraphCheckpointAdapter",
    "PostgresCheckpointProbe",
    "configure_windows_asyncio_policy",
    "checkpoint_config",
    "checkpoint_dsn",
    "checkpoint_thread_id",
]
