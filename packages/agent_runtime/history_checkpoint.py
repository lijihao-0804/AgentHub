"""Bind checkpoint history to the exact frozen Run source and plan."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.agent_runtime.work_layer import ThreadHistorySourceManifest
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError


def history_checkpoint_binding(
    *,
    workspace_id: UUID,
    thread_id: UUID,
    run_id: UUID,
    agent_version_id: UUID,
    resolved_spec_hash: str,
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the private identity of a model-visible frozen history plan."""
    if (
        not isinstance(workspace_id, UUID)
        or not isinstance(thread_id, UUID)
        or not isinstance(run_id, UUID)
        or not isinstance(agent_version_id, UUID)
        or not isinstance(resolved_spec_hash, str)
        or not isinstance(snapshot, Mapping)
        or not isinstance(snapshot.get("source"), Mapping)
        or not isinstance(snapshot.get("plan"), Mapping)
    ):
        raise ValueError("history binding is incomplete")
    manifest = ThreadHistorySourceManifest.from_manifest(snapshot["source"])
    if (
        manifest.scope.workspace_id != workspace_id
        or manifest.scope.thread_id != thread_id
        or manifest.scope.before_run_id != run_id
    ):
        raise ValueError("history binding scope differs")
    return {
        "contract_version": 1,
        "workspace_id": str(workspace_id),
        "thread_id": str(thread_id),
        "run_id": str(run_id),
        "agent_version_id": str(agent_version_id),
        "resolved_spec_hash": resolved_spec_hash,
        "source_manifest_sha256": manifest.manifest_sha256,
        "plan_sha256": canonical_json_hash(dict(snapshot["plan"])),
    }


async def verify_history_checkpoint_binding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run: AgentRun,
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Recheck checkpoint history before another model or a resumed action.

    Old M3 checkpoints without this field keep their prior behavior. A
    summary-enabled checkpoint has no such fallback. This validates the
    checkpoint state binding, not an exact LangGraph checkpoint ID.
    """
    binding = state.get("history_checkpoint_binding")
    if binding is None:
        runtime = state.get("runtime")
        policy = runtime.get("summary_policy") if isinstance(runtime, Mapping) else None
        if isinstance(policy, Mapping) and policy.get("enabled") is True:
            raise _invalid_history_binding()
        return None
    async with session_factory() as session:
        row = (
            await session.execute(
                select(AgentRun, AgentVersion)
                .join(
                    AgentVersion,
                    (AgentVersion.workspace_id == AgentRun.workspace_id)
                    & (AgentVersion.id == AgentRun.agent_version_id),
                )
                .where(AgentRun.workspace_id == run.workspace_id, AgentRun.id == run.id)
            )
        ).one_or_none()
    try:
        if row is None:
            raise ValueError("missing Run or version")
        persisted, version = row
        if (
            persisted.thread_id != run.thread_id
            or persisted.agent_version_id != run.agent_version_id
            or canonical_json_hash(version.resolved_spec) != version.resolved_spec_hash
            or (
                persisted.resolved_spec_hash is not None
                and persisted.resolved_spec_hash != version.resolved_spec_hash
            )
        ):
            raise ValueError("Run identity or version changed")
        expected = history_checkpoint_binding(
            workspace_id=persisted.workspace_id,
            thread_id=persisted.thread_id,
            run_id=persisted.id,
            agent_version_id=persisted.agent_version_id,
            resolved_spec_hash=version.resolved_spec_hash,
            snapshot=persisted.effective_thread_history_snapshot,
        )
        if binding != expected:
            raise ValueError("checkpoint history binding changed")
        return persisted.effective_thread_history_snapshot
    except (KeyError, TypeError, ValueError):
        raise _invalid_history_binding() from None


def _invalid_history_binding() -> AgentHubError:
    return AgentHubError(
        "AGENT_HISTORY_SNAPSHOT_INVALID",
        "The checkpoint does not match the frozen thread history.",
        422,
    )
