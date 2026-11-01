"""Bind checkpoint history to the exact frozen Run source and plan."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.adapters.langgraph.checkpoint import checkpoint_thread_id
from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.agent_runtime.work_layer import ThreadHistorySourceManifest
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError

SUMMARY_BOUNDARY_VERSION = "summary_dispatch_boundary_v1"


def summary_boundary_marker(model_round_count: int) -> dict[str, Any]:
    """Mark a graph-produced point immediately before a model round."""
    if type(model_round_count) is not int or not 0 <= model_round_count < 2_147_483_647:
        raise ValueError("invalid completed model-round count")
    return {"version": SUMMARY_BOUNDARY_VERSION, "model_round": model_round_count + 1}


class ExactCheckpointReader(Protocol):
    """The framework operations needed to read one explicitly selected tuple."""

    async def aget_tuple(self, config: Mapping[str, Any]) -> Any: ...


class ExactCheckpointAdapter(Protocol):
    """Adapter boundary kept structural so callers own framework construction."""

    def checkpointer(self) -> AbstractAsyncContextManager[ExactCheckpointReader]: ...

    def config_for_checkpoint(
        self,
        workspace_id: UUID | str,
        run_id: UUID | str,
        *,
        checkpoint_id: str,
        checkpoint_ns: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class VerifiedCheckpointBinding:
    """Server-derived identity for one exact, history-bound LangGraph tuple."""

    workspace_id: UUID
    thread_id: UUID
    run_id: UUID
    agent_version_id: UUID
    checkpoint_id: str
    checkpoint_ns: str
    checkpoint_model_round_count: int
    history_binding_sha256: str
    checkpoint_binding_sha256: str


@dataclass(frozen=True, slots=True)
class PersistedCheckpointBinding:
    """The fixed fields a durable summary job retains from a verified tuple."""

    workspace_id: UUID
    thread_id: UUID
    run_id: UUID
    checkpoint_id: str
    checkpoint_ns: str
    checkpoint_model_round_count: int
    checkpoint_binding_sha256: str


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


async def verify_exact_checkpoint_binding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    checkpoint_adapter: ExactCheckpointAdapter,
    workspace_id: UUID,
    thread_id: UUID,
    run_id: UUID,
    checkpoint_id: str,
    checkpoint_ns: str,
) -> VerifiedCheckpointBinding:
    """Read and prove one tuple selected by the caller.

    The supplied checkpoint ID and namespace are selectors, never evidence.
    The returned identity comes only from an exact tuple read after the
    authoritative tenant-scoped Run/Version lookup. This function deliberately
    has no latest-checkpoint fallback and requires the frozen history binding
    even when a legacy M3 checkpoint could otherwise use compatibility mode.
    """

    try:
        if not all(
            isinstance(value, UUID) for value in (workspace_id, thread_id, run_id)
        ):
            raise ValueError("checkpoint scope must contain UUIDs")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("checkpoint ID must be a non-empty string")
        if not isinstance(checkpoint_ns, str):
            raise ValueError("checkpoint namespace must be a string")

        async with session_factory() as session:
            row = (
                await session.execute(
                    select(AgentRun, AgentVersion)
                    .join(
                        AgentVersion,
                        (AgentVersion.workspace_id == AgentRun.workspace_id)
                        & (AgentVersion.id == AgentRun.agent_version_id),
                    )
                    .where(
                        AgentRun.workspace_id == workspace_id,
                        AgentRun.id == run_id,
                    )
                )
            ).one_or_none()
        if row is None:
            raise ValueError("missing Run or version")
        persisted, version = row
        if (
            persisted.thread_id != thread_id
            or persisted.agent_version_id != version.id
        ):
            raise ValueError("checkpoint Run scope changed")

        selector_config = checkpoint_adapter.config_for_checkpoint(
            persisted.workspace_id,
            persisted.id,
            checkpoint_id=checkpoint_id,
            checkpoint_ns=checkpoint_ns,
        )
        selector_configurable = _configurable(selector_config)
        if (
            selector_configurable.get("thread_id")
            != checkpoint_thread_id(persisted.workspace_id, persisted.id)
            or selector_configurable.get("checkpoint_id") != checkpoint_id
            or selector_configurable.get("checkpoint_ns") != checkpoint_ns
        ):
            raise ValueError("checkpoint adapter changed the exact selector")

        async with checkpoint_adapter.checkpointer() as checkpointer:
            tuple_ = await checkpointer.aget_tuple(selector_config)
        if tuple_ is None:
            raise ValueError("exact checkpoint is missing")
        tuple_configurable = _configurable(getattr(tuple_, "config", None))
        if (
            tuple_configurable.get("thread_id")
            != checkpoint_thread_id(persisted.workspace_id, persisted.id)
            or tuple_configurable.get("checkpoint_id") != checkpoint_id
            or tuple_configurable.get("checkpoint_ns") != checkpoint_ns
        ):
            raise ValueError("checkpoint tuple differs from the exact selector")

        checkpoint_data = getattr(tuple_, "checkpoint", None)
        if not isinstance(checkpoint_data, Mapping):
            raise ValueError("checkpoint payload is malformed")
        if checkpoint_data.get("id") != checkpoint_id:
            raise ValueError("checkpoint payload ID differs")
        channels = checkpoint_data.get("channel_values")
        if not isinstance(channels, Mapping):
            raise ValueError("checkpoint channels are malformed")
        if not isinstance(channels.get("history_checkpoint_binding"), Mapping):
            raise ValueError("summary checkpoint has no history binding")
        model_round_count = channels.get("model_round_count")
        if (
            isinstance(model_round_count, bool)
            or not isinstance(model_round_count, int)
            or not 0 <= model_round_count <= 2_147_483_647
        ):
            raise ValueError("checkpoint model-round count is invalid")
        if channels.get("summary_boundary") != summary_boundary_marker(model_round_count):
            raise ValueError("checkpoint is not a recorded model dispatch boundary")
        if channels.get("failure_code") not in (None, "AGENT_HISTORY_CONTEXT_INSUFFICIENT"):
            raise ValueError("checkpoint carries a terminal failure")
        if channels.get("run_status") not in (None, "RUNNING"):
            raise ValueError("checkpoint is not eligible for a new dispatch")
        if channels.get("pending_tool_calls") or channels.get("proposed_tool_calls"):
            raise ValueError("checkpoint has an incomplete tool exchange")

        snapshot = await verify_history_checkpoint_binding(
            session_factory,
            run=persisted,
            state=channels,
        )
        if not isinstance(snapshot, Mapping):
            raise ValueError("summary checkpoint did not bind frozen history")
        history_binding_sha256 = canonical_json_hash(
            dict(channels["history_checkpoint_binding"])
        )
        checkpoint_identity = {
            "contract_version": "exact_langgraph_checkpoint_v1",
            "workspace_id": str(persisted.workspace_id),
            "thread_id": str(persisted.thread_id),
            "run_id": str(persisted.id),
            "agent_version_id": str(persisted.agent_version_id),
            "checkpoint_thread_id": checkpoint_thread_id(
                persisted.workspace_id, persisted.id
            ),
            "checkpoint_id": checkpoint_id,
            "checkpoint_ns": checkpoint_ns,
            "model_round_count": model_round_count,
            "summary_boundary": dict(channels["summary_boundary"]),
            "history_binding_sha256": history_binding_sha256,
        }
        return VerifiedCheckpointBinding(
            workspace_id=persisted.workspace_id,
            thread_id=persisted.thread_id,
            run_id=persisted.id,
            agent_version_id=persisted.agent_version_id,
            checkpoint_id=checkpoint_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_model_round_count=model_round_count,
            history_binding_sha256=history_binding_sha256,
            checkpoint_binding_sha256=canonical_json_hash(checkpoint_identity),
        )
    except AgentHubError:
        raise _invalid_checkpoint_binding() from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise _invalid_checkpoint_binding() from None
    except Exception:
        # Framework read errors and adapter failures are not proof of absence;
        # both deny the binding without trying a latest-checkpoint fallback.
        raise _invalid_checkpoint_binding() from None


async def verify_fixed_checkpoint_binding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    checkpoint_adapter: ExactCheckpointAdapter,
    expected: VerifiedCheckpointBinding | PersistedCheckpointBinding,
) -> VerifiedCheckpointBinding:
    """Re-read one persisted identity exactly and require byte-for-byte equality."""

    if not isinstance(
        expected, (VerifiedCheckpointBinding, PersistedCheckpointBinding)
    ):
        raise _invalid_checkpoint_binding()
    actual = await verify_exact_checkpoint_binding(
        session_factory,
        checkpoint_adapter=checkpoint_adapter,
        workspace_id=expected.workspace_id,
        thread_id=expected.thread_id,
        run_id=expected.run_id,
        checkpoint_id=expected.checkpoint_id,
        checkpoint_ns=expected.checkpoint_ns,
    )
    if isinstance(expected, VerifiedCheckpointBinding):
        matches = actual == expected
    else:
        matches = (
            actual.workspace_id == expected.workspace_id
            and actual.thread_id == expected.thread_id
            and actual.run_id == expected.run_id
            and actual.checkpoint_id == expected.checkpoint_id
            and actual.checkpoint_ns == expected.checkpoint_ns
            and actual.checkpoint_model_round_count
            == expected.checkpoint_model_round_count
            and actual.checkpoint_binding_sha256
            == expected.checkpoint_binding_sha256
        )
    if not matches:
        raise _invalid_checkpoint_binding()
    return actual


async def verify_history_checkpoint_binding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run: AgentRun,
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Recheck checkpoint history before another model or a resumed action.

    Old M3 checkpoints without this field keep their prior behavior after
    checking the authoritative version. A summary-enabled version has no
    such fallback, even if its checkpoint's runtime field is absent. This
    validates the state binding, not an exact LangGraph checkpoint ID.
    """
    binding = state.get("history_checkpoint_binding")
    if binding is None:
        runtime = state.get("runtime")
        policy = runtime.get("summary_policy") if isinstance(runtime, Mapping) else None
        if isinstance(policy, Mapping) and policy.get("enabled") is True:
            raise _invalid_history_binding()
        if run.thread_id is None:
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
        if binding is None:
            spec = version.resolved_spec
            runtime = spec.get("runtime") if isinstance(spec, Mapping) else None
            if not isinstance(runtime, Mapping) or runtime.get("summary_policy") is not None:
                raise ValueError("summary-enabled version has no checkpoint binding")
            return None
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


def _invalid_checkpoint_binding() -> AgentHubError:
    return AgentHubError(
        "AGENT_CHECKPOINT_BINDING_INVALID",
        "The exact LangGraph checkpoint could not be verified.",
        409,
    )


def _configurable(config: Any) -> Mapping[str, Any]:
    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    if not isinstance(configurable, Mapping):
        raise ValueError("checkpoint config is malformed")
    return configurable


__all__ = [
    "ExactCheckpointAdapter",
    "ExactCheckpointReader",
    "PersistedCheckpointBinding",
    "VerifiedCheckpointBinding",
    "history_checkpoint_binding",
    "summary_boundary_marker",
    "verify_exact_checkpoint_binding",
    "verify_fixed_checkpoint_binding",
    "verify_history_checkpoint_binding",
]
