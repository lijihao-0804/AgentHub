"""AgentRunService: orchestration, persistence, streaming and frozen resolution."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.adapters.langgraph import (
    LangGraphCheckpointAdapter,
    interrupt_payloads,
    resume_command,
)
from packages.agent_runtime.event_store import RunEventReader, RunEventRecorder
from packages.agent_runtime.events import AgentEvent, AgentEventEmitter, AgentEventType
from packages.agent_runtime.evidence_registry import visible_evidence_registry
from packages.agent_runtime.execution_lease import run_execution_lease
from packages.agent_runtime.frozen import FrozenAgentSpec, parse_frozen_agent_spec
from packages.agent_runtime.history_checkpoint import verify_history_checkpoint_binding
from packages.agent_runtime.history_transcript import MAX_ENCODED_BYTES, build_turn_transcript
from packages.agent_runtime.models import AgentRun, AgentVersion, RunStep
from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.agent_runtime.runtime_projection import (
    DEFAULT_STREAM_GRACE_SECONDS,
    DEFAULT_THREAD_CONTEXT_MAX_TURNS,
    STREAM_FOLLOW_STOP_STATUSES,
    AgentRunExecutionOverrides,
    AgentRunResult,
    AgentRunState,
    _aggregate_usage,
    _approval_interrupt_payload,
    _assert_wiki_publication_not_revoked,
    _close_event_queue,
    _is_uncertain_action_failure,
    _queue_event,
    _run_result,
    _safe_end_span,
    _safe_start_span,
)
from packages.agent_runtime.stream_hub import RunStreamHub, RunStreamRegistry
from packages.agent_runtime.work_layer import (
    RunArtifactRecorder,
    ThreadContextProvider,
)
from packages.approvals import ApprovalExecutionStatus, ApprovalService
from packages.approvals.models import Approval
from packages.control_plane.services import TenantService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import PrincipalContext, WorkspaceExecutionContext
from packages.knowledge.models import KnowledgeSnapshot
from packages.knowledge.snapshots import KnowledgeSnapshotService
from packages.memory.contracts import (
    MemorySelector,
    MemoryWriteQueue,
)
from packages.model_gateway.contracts import (
    ModelGateway,
)
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.errors import ModelGatewayError
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.observability import NoopTraceSink
from packages.observability.contracts import TraceSink
from packages.threads.models import AgentThread, ThreadTurn
from packages.tools.actions import ActionRuntime
from packages.tools.runtime import ToolRuntime
from packages.wiki.models import WikiCompilation

logger = logging.getLogger(__name__)


class AgentRunService:
    """Owns short persistence transactions around an in-memory graph run."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        model_gateway_factory: Callable[[AsyncSession], ModelGateway] | None = None,
        tool_runtime: ToolRuntime | None = None,
        trace_sink: TraceSink | None = None,
        credential_cipher: ProviderCredentialCipher | None = None,
        approval_service: ApprovalService | None = None,
        action_runtime: ActionRuntime | None = None,
        checkpoint_adapter: LangGraphCheckpointAdapter | None = None,
        thread_context_provider: ThreadContextProvider | None = None,
        memory_selector: MemorySelector | None = None,
        memory_writer: MemoryWriteQueue | None = None,
        artifact_recorder: RunArtifactRecorder | None = None,
        thread_context_max_turns: int = DEFAULT_THREAD_CONTEXT_MAX_TURNS,
        stream_grace_seconds: float = DEFAULT_STREAM_GRACE_SECONDS,
        stream_registry: RunStreamRegistry | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.trace_sink = trace_sink or NoopTraceSink()
        self.model_gateway_factory = model_gateway_factory or (
            lambda session: SqlAlchemyModelGateway(
                session,
                credential_cipher=credential_cipher,
                trace_sink=self.trace_sink,
            )
        )
        self.tool_runtime = tool_runtime or ToolRuntime(session_factory=session_factory)
        self.approval_service = approval_service
        self.action_runtime = action_runtime
        self.checkpoint_adapter = checkpoint_adapter
        self.thread_context_provider = thread_context_provider
        # Both halves of long-term memory are optional collaborators. A
        # deployment that wires neither behaves exactly as it did before the
        # feature existed, which is also what every unit test relies on.
        self.memory_selector = memory_selector
        self.memory_writer = memory_writer
        self.artifact_recorder = artifact_recorder
        self.thread_context_max_turns = thread_context_max_turns
        self.stream_grace_seconds = stream_grace_seconds
        # A run is owned by its hub, not by whoever is currently reading it, so
        # that a dropped connection is not the same thing as "stop the run".
        self.stream_registry = stream_registry or RunStreamRegistry()
        self.event_reader = RunEventReader(session_factory)

    async def run(
        self,
        context: WorkspaceExecutionContext,
        *,
        agent_version_id: UUID,
        input_text: str,
        thread_id: UUID | None = None,
        thread_turn_id: UUID | None = None,
    ) -> AgentRunResult:
        self._require_permission(context, "agent_run")
        run = await self.prepare_run(
            context,
            agent_version_id=agent_version_id,
            input_text=input_text,
            thread_id=thread_id,
            thread_turn_id=thread_turn_id,
        )
        return await self.execute_prepared_run(context, run_id=run.id)

    async def prepare_run(
        self,
        context: WorkspaceExecutionContext,
        *,
        agent_version_id: UUID,
        input_text: str,
        execution_overrides: AgentRunExecutionOverrides | None = None,
        thread_id: UUID | None = None,
        thread_turn_id: UUID | None = None,
    ) -> AgentRun:
        """Persist a durable AgentRun before any external model/tool work begins."""

        self._require_permission(context, "agent_run")
        return await self._create_run(
            context,
            agent_version_id,
            input_text,
            execution_overrides=execution_overrides,
            thread_id=thread_id,
            thread_turn_id=thread_turn_id,
        )

    async def execute_prepared_run(
        self, context: WorkspaceExecutionContext, *, run_id: UUID
    ) -> AgentRunResult:
        self._require_permission(context, "agent_run")
        async with run_execution_lease(
            self.session_factory,
            UUID(context.workspace_id),
            run_id,
            allowed_statuses={"RUNNING", "CANCEL_REQUESTED"},
        ) as owner:
            if owner is None:
                return _run_result(await self.get_run(context, run_id))
            return await self._execute_prepared_run_owned(context, run_id=run_id)

    async def _execute_prepared_run_owned(
        self, context: WorkspaceExecutionContext, *, run_id: UUID
    ) -> AgentRunResult:
        """Execute an already persisted run; terminal runs are never duplicated."""

        self._require_permission(context, "agent_run")
        async with self.session_factory() as session:
            run = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == UUID(context.workspace_id), AgentRun.id == run_id
                )
            )
        if run is None:
            raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
        if run.status == "CANCEL_REQUESTED":
            return await self._complete_run(
                context,
                run.id,
                status="CANCELLED",
                final_output=None,
                failure_code=None,
                model_step_count=run.model_step_count,
                tool_call_count=run.tool_call_count,
                usage_records=[],
            )
        if run.status != "RUNNING":
            return _run_result(run)
        if not await self._frozen_wiki_publications_intact(run):
            return await self._complete_run(
                context,
                run.id,
                status="NEEDS_ATTENTION",
                final_output=None,
                failure_code="WIKI_PUBLICATION_UNAVAILABLE",
                model_step_count=run.model_step_count,
                tool_call_count=run.tool_call_count,
                usage_records=[],
            )
        agent_version_id = run.agent_version_id
        started = time.perf_counter()
        span = await _safe_start_span(
            self.trace_sink,
            "agent.run",
            {
                "workspace_id": context.workspace_id,
                "agent_version_id": str(agent_version_id),
                "run_id": str(run.id),
            },
        )
        state = self._initial_state(run, agent_version_id)
        graph: _AgentRunGraph | None = None
        try:
            graph = _AgentRunGraph(self, context, run)
            final_state = await self._invoke_graph(graph, state, run)
            state = final_state
            approval_payload = _approval_interrupt_payload(final_state)
            if approval_payload is not None:
                await self._mark_waiting(context, run.id)
                result = await self._waiting_result(
                    context,
                    run,
                    model_step_count=int(state.get("model_round_count", 0)),
                    tool_call_count=int(state.get("tool_call_count", 0)),
                    usage_records=state.get("usage_records", []),
                    waiting_approval_id=UUID(str(approval_payload["approval_id"])),
                )
                await _safe_end_span(
                    span,
                    {
                        "workspace_id": context.workspace_id,
                        "agent_version_id": str(agent_version_id),
                        "run_id": str(run.id),
                        "status": result.status,
                        "approval_id": approval_payload.get("approval_id"),
                        "logical_action_id": approval_payload.get("logical_action_id"),
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    },
                    status="ok",
                    failure_code=None,
                )
                return result
            failure_code = final_state.get("failure_code")
            status = final_state.get("run_status") or ("FAILED" if failure_code else "SUCCEEDED")
            final_output = final_state.get("final_output") if not failure_code else None
        except AgentHubError as error:
            failure_code = error.code
            status = "FAILED"
            final_output = None
        except ModelGatewayError as error:
            failure_code = error.code.value
            status = "FAILED"
            final_output = None
        except Exception:
            logger.warning("agent_run_failed", exc_info=True)
            failure_code = "AGENT_RUN_FAILED"
            status = "FAILED"
            final_output = None
        result = await self._complete_run(
            context,
            run.id,
            status=status,
            final_output=final_output,
            failure_code=failure_code,
            model_step_count=int(state.get("model_round_count", 0)),
            tool_call_count=int(state.get("tool_call_count", 0)),
            usage_records=state.get("usage_records", []),
            turn_transcript=state.get("turn_transcript"),
            turn_evidence_registry=state.get("turn_evidence_registry"),
            pending_finish=graph.pending_finish if graph is not None else None,
        )
        await _safe_end_span(
            span,
            {
                "workspace_id": context.workspace_id,
                "agent_version_id": str(agent_version_id),
                "run_id": str(run.id),
                "status": result.status,
                "model_step_count": result.model_step_count,
                "tool_call_count": result.tool_call_count,
                "failure_code": result.failure_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            },
            status="error" if result.failure_code else "ok",
            failure_code=result.failure_code,
        )
        return result

    async def _invoke_graph(
        self, graph: _AgentRunGraph, state: AgentRunState | Any, run: AgentRun
    ) -> AgentRunState:
        if self.approval_service is None or self.checkpoint_adapter is None:
            return await graph.invoke(state)
        async with self.checkpoint_adapter.checkpointer() as checkpointer:
            return await graph.invoke(
                state,
                checkpointer=checkpointer,
                config=self.checkpoint_adapter.config_for_run(run.workspace_id, run.id),
            )

    async def resume(
        self,
        context: WorkspaceExecutionContext,
        *,
        run_id: UUID,
        approval_id: UUID,
    ) -> AgentRunResult:
        self._require_permission(context, "agent_run")
        async with run_execution_lease(
            self.session_factory,
            UUID(context.workspace_id),
            run_id,
            allowed_statuses={"WAITING_APPROVAL"},
        ) as owner:
            if owner is None:
                return _run_result(await self.get_run(context, run_id))
            return await self._resume_owned(context, run_id=run_id, approval_id=approval_id)

    async def _resume_owned(
        self, context: WorkspaceExecutionContext, *, run_id: UUID, approval_id: UUID
    ) -> AgentRunResult:
        """Resume the same durable Run after a decision has been persisted."""

        self._require_permission(context, "agent_run")
        run: AgentRun | None = None
        runtime_context: WorkspaceExecutionContext | None = None
        approval: Approval | None = None
        graph: _AgentRunGraph | None = None
        recorder: RunEventRecorder | None = None
        result: AgentRunResult | None = None
        raised = False
        normal_return = False
        inactive_approval_request = False
        try:
            async with self.session_factory() as session:
                run = await session.scalar(
                    select(AgentRun)
                    .where(
                        AgentRun.workspace_id == UUID(context.workspace_id),
                        AgentRun.id == run_id,
                    )
                    .with_for_update()
                )
                if run is None:
                    raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
                if run.status != "WAITING_APPROVAL":
                    normal_return = True
                    return _run_result(run)
            runtime_context = await self._authoritative_run_context(context, run)
            if self.approval_service is None or self.checkpoint_adapter is None:
                raise AgentHubError(
                    "CHECKPOINT_NOT_CONFIGURED",
                    "Durable approval resume is not configured.",
                    503,
                )
            approval = await self._load_resume_approval(runtime_context, run, approval_id)
            if run.waiting_approval_id is not None and run.waiting_approval_id != approval_id:
                inactive_approval_request = True
                raise AgentHubError(
                    "APPROVAL_RESUME_MISMATCH",
                    "The approval is not the active durable interrupt.",
                    409,
                )
            checkpoint_error = await self._validate_checkpoint_interrupt(
                run, approval_id=approval_id
            )
            if checkpoint_error is not None:
                raise AgentHubError(
                    checkpoint_error,
                    "The durable approval checkpoint is invalid.",
                    409,
                )
            if not await self._frozen_wiki_publications_intact(run):
                raise AgentHubError(
                    "WIKI_PUBLICATION_UNAVAILABLE",
                    "The frozen Wiki publication is no longer available.",
                    409,
                )
            required_permissions = await self._resume_required_permissions(run, approval)
            if not required_permissions.issubset(runtime_context.permissions):
                return await self._fail_resume_safely(
                    runtime_context,
                    run.id,
                    "RUN_ACTOR_ACCESS_REVOKED",
                )
            if self.action_runtime is not None:
                try:
                    runtime_context = await self._authoritative_run_context(runtime_context, run)
                except AgentHubError as error:
                    if error.status_code not in {403, 404}:
                        raise
                    return await self._fail_resume_safely(
                        context,
                        run.id,
                        "RUN_ACTOR_ACCESS_REVOKED",
                    )
                required_permissions = await self._resume_required_permissions(run, approval)
                if not required_permissions.issubset(runtime_context.permissions):
                    return await self._fail_resume_safely(
                        runtime_context,
                        run.id,
                        "RUN_ACTOR_ACCESS_REVOKED",
                    )
            if not await self._transition_resume_to_running(run):
                persisted = await self._load_run(runtime_context, run.id)
                normal_return = True
                return _run_result(persisted)
            recorder = RunEventRecorder(
                self.session_factory, workspace_id=run.workspace_id, run_id=run.id
            )
            emitter = AgentEventEmitter(
                run_id=str(run.id),
                agent_version_id=str(run.agent_version_id),
                request_id=runtime_context.request_id,
                initial_sequence=await self.event_reader.latest_sequence(
                    workspace_id=run.workspace_id, run_id=run.id
                ),
            )
            graph = _AgentRunGraph(
                self,
                runtime_context,
                run,
                emitter=emitter,
                event_recorder=recorder,
            )
            async with self.checkpoint_adapter.checkpointer() as checkpointer:
                final_state = await graph.invoke(
                    resume_command({"approval_id": str(approval_id)}),
                    checkpointer=checkpointer,
                    config=self.checkpoint_adapter.config_for_run(run.workspace_id, run.id),
                )
            approval_payload = _approval_interrupt_payload(final_state)
            if approval_payload is not None:
                result = await self._waiting_result(
                    runtime_context,
                    run,
                    model_step_count=int(final_state.get("model_round_count", 0)),
                    tool_call_count=int(final_state.get("tool_call_count", 0)),
                    usage_records=final_state.get("usage_records", []),
                    waiting_approval_id=UUID(str(approval_payload["approval_id"])),
                )
            else:
                failure_code = final_state.get("failure_code")
                status = final_state.get("run_status") or (
                    "FAILED" if failure_code else "SUCCEEDED"
                )
                result = await self._complete_run(
                    runtime_context,
                    run.id,
                    status=status,
                    final_output=final_state.get("final_output") if not failure_code else None,
                    failure_code=failure_code,
                    model_step_count=graph.model_round_count,
                    tool_call_count=graph.tool_call_count,
                    usage_records=graph.usage_records,
                    turn_transcript=final_state.get("turn_transcript"),
                    turn_evidence_registry=final_state.get("turn_evidence_registry"),
                    pending_finish=graph.pending_finish,
                )
        except AgentHubError as error:
            raised = True
            if run is None:
                raise
            if runtime_context is None and error.status_code not in {403, 404}:
                raise
            failure_code = (
                "RUN_ACTOR_ACCESS_REVOKED"
                if runtime_context is None
                else error.code
                if error.code
                in {
                    "APPROVAL_CHECKPOINT_INVALID",
                    "APPROVAL_CHECKPOINT_MISSING",
                    "APPROVAL_RESUME_MISMATCH",
                    "AGENT_HISTORY_SNAPSHOT_INVALID",
                    "WIKI_PUBLICATION_UNAVAILABLE",
                }
                else "RUN_EXECUTION_INTERRUPTED"
            )
            if runtime_context is None:
                try:
                    await asyncio.shield(self._fail_resume_safely(context, run.id, failure_code))
                finally:
                    if recorder is not None:
                        await recorder.aclose()
                normal_return = True
                return _run_result(await self._load_run(context, run.id))
            if inactive_approval_request:
                if recorder is not None:
                    await recorder.aclose()
                raise
            try:
                await asyncio.shield(
                    self._fail_resume_safely(runtime_context, run.id, failure_code)
                )
            finally:
                if recorder is not None:
                    await recorder.aclose()
            raise
        except BaseException:
            raised = True
            if recorder is not None:
                recorder._failed = True
            try:
                if runtime_context is not None and run is not None:
                    await asyncio.shield(
                        self._fail_resume_safely(
                            runtime_context,
                            run.id,
                            "RUN_EXECUTION_INTERRUPTED",
                        )
                    )
            finally:
                if recorder is not None:
                    await recorder.aclose()
            raise
        finally:
            if raised is False and normal_return and recorder is not None:
                await recorder.aclose()
        if result is None:
            raise AgentHubError(
                "RUN_EXECUTION_INTERRUPTED", "The resumed run produced no terminal state.", 500
            )
        event_type = (
            AgentEventType.RUN_COMPLETED
            if result.status == "SUCCEEDED"
            else AgentEventType.RUN_CANCELLED
            if result.status == "CANCELLED"
            else AgentEventType.RUN_FAILED
        )
        data = {
            "status": result.status,
            "model_step_count": result.model_step_count,
            "tool_call_count": result.tool_call_count,
        }
        if event_type is AgentEventType.RUN_COMPLETED:
            data["output"] = result.final_output or ""
        elif event_type is AgentEventType.RUN_FAILED:
            data["failure_code"] = result.failure_code or "AGENT_RUN_FAILED"
        try:
            if graph is not None:
                await graph._emit(event_type, data)
        finally:
            if recorder is not None:
                await recorder.aclose()
        return result

    async def _load_resume_approval(
        self,
        context: WorkspaceExecutionContext,
        run: AgentRun,
        approval_id: UUID,
    ) -> Approval:
        """Read the durable interrupt without using a missing read grant as the fence."""

        workspace_id = UUID(context.workspace_id)
        async with self.session_factory() as session:
            approval = await session.scalar(
                select(Approval).where(
                    Approval.workspace_id == workspace_id,
                    Approval.id == approval_id,
                )
            )
        if approval is None:
            raise AgentHubError(
                "APPROVAL_RESUME_MISMATCH",
                "The approval does not match the durable run.",
                409,
            )
        if approval.run_id != run.id:
            raise AgentHubError(
                "APPROVAL_RESUME_MISMATCH",
                "The approval does not belong to this durable run.",
                409,
            )
        if approval.agent_version_id != run.agent_version_id:
            raise AgentHubError(
                "APPROVAL_RESUME_MISMATCH",
                "The approval does not match the run's published version.",
                409,
            )
        return approval

    async def _load_run(self, context: WorkspaceExecutionContext, run_id: UUID) -> AgentRun:
        """Load one Run for internal lifecycle paths that already hold agent_run."""

        async with self.session_factory() as session:
            run = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == UUID(context.workspace_id),
                    AgentRun.id == run_id,
                )
            )
        if run is None:
            raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
        return run

    async def _validate_checkpoint_interrupt(
        self, run: AgentRun, *, approval_id: UUID
    ) -> str | None:
        """Require exactly the active approval identity in the pending checkpoint."""

        if self.checkpoint_adapter is None:
            return "APPROVAL_CHECKPOINT_MISSING"
        try:
            async with self.checkpoint_adapter.checkpointer() as checkpointer:
                checkpoint = await checkpointer.aget_tuple(
                    self.checkpoint_adapter.config_for_run(run.workspace_id, run.id)
                )
        except Exception:
            logger.warning("approval_checkpoint_read_failed", exc_info=True)
            return "APPROVAL_CHECKPOINT_INVALID"
        if checkpoint is None:
            return "APPROVAL_CHECKPOINT_MISSING"
        pending = []
        for write in getattr(checkpoint, "pending_writes", None) or ():
            try:
                channel, value = write[1], write[2]
            except (IndexError, TypeError):
                continue
            if channel == "__interrupt__":
                pending.extend(value if isinstance(value, (list, tuple)) else (value,))
        if not pending:
            return "APPROVAL_CHECKPOINT_INVALID"
        try:
            payloads = interrupt_payloads({"__interrupt__": pending})
            interrupt_ids = {
                str(UUID(str(payload["approval_id"])))
                for payload in payloads
                if payload.get("approval_id") is not None
            }
        except (KeyError, TypeError, ValueError):
            return "APPROVAL_CHECKPOINT_INVALID"
        expected = str(approval_id)
        if (
            len(payloads) != 1
            or interrupt_ids != {expected}
            or (run.waiting_approval_id is not None and str(run.waiting_approval_id) != expected)
        ):
            return "APPROVAL_RESUME_MISMATCH"
        checkpoint_data = getattr(checkpoint, "checkpoint", {})
        channels = (
            checkpoint_data.get("channel_values", {})
            if isinstance(checkpoint_data, Mapping)
            else {}
        )
        if not isinstance(channels, Mapping):
            return "APPROVAL_CHECKPOINT_INVALID"
        try:
            await verify_history_checkpoint_binding(self.session_factory, run=run, state=channels)
        except AgentHubError as error:
            return error.code
        except Exception:
            logger.warning("approval_history_checkpoint_verification_failed", exc_info=True)
            return "APPROVAL_CHECKPOINT_INVALID"
        return None

    async def _resume_required_permissions(
        self, run: AgentRun, approval: Approval
    ) -> frozenset[str]:
        """Return the exact current grants required before a resumed action can claim."""

        required = {"agent_run"}
        if approval.execution_status in {
            ApprovalExecutionStatus.SUCCEEDED,
            ApprovalExecutionStatus.FAILED,
            ApprovalExecutionStatus.UNKNOWN_OUTCOME,
        }:
            return frozenset(required)
        required.update({"workspace_read", "tool_run"})
        if approval.tool_identity in {"search_knowledge", "query_wiki"}:
            required.add("knowledge_run")
        return frozenset(required)

    async def _transition_resume_to_running(self, run: AgentRun) -> bool:
        """Atomically consume WAITING_APPROVAL; return false after a racing transition."""

        async with self.session_factory() as session:
            current = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.workspace_id == run.workspace_id,
                    AgentRun.id == run.id,
                )
                .with_for_update()
            )
            if current is None or current.status != "WAITING_APPROVAL":
                return False
            current.status = "RUNNING"
            current.event_log_complete = False
            await session.commit()
            return True

    async def _fail_resume_safely(
        self,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        failure_code: str,
    ) -> AgentRunResult:
        """Converge a failed resume without rewriting confirmed action state."""

        async with self.session_factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.workspace_id == UUID(context.workspace_id),
                    AgentRun.id == run_id,
                )
                .with_for_update()
            )
            if run is None:
                raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
            claimed_action = await session.scalar(
                select(Approval.id)
                .where(
                    Approval.workspace_id == run.workspace_id,
                    Approval.run_id == run.id,
                    Approval.execution_status.in_({"CLAIMED", "UNKNOWN_OUTCOME"}),
                )
                .limit(1)
            )
            if isinstance(claimed_action, UUID):
                failure_code = "ACTION_RECONCILIATION_REQUIRED"
            if run.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return _run_result(run)
            if (
                run.status == "NEEDS_ATTENTION"
                and run.failure_code
                and failure_code == "RUN_EXECUTION_INTERRUPTED"
            ):
                failure_code = run.failure_code
            run.status = "NEEDS_ATTENTION"
            run.failure_code = failure_code
            run.final_output = None
            run.event_log_complete = False
            run.resume_pending = False
            run.waiting_approval_id = None
            run.completed_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(run)
            return _run_result(run)

    async def _authoritative_run_context(
        self, request_context: WorkspaceExecutionContext, run: AgentRun
    ) -> WorkspaceExecutionContext:
        """Re-resolve the original run actor before graph resume.

        The approver remains the audit actor for the decision endpoint; the
        resumed graph executes under the run creator's current membership and
        permissions, never under elevated approver privileges.
        """

        principal = PrincipalContext(
            request_id=request_context.request_id,
            trace_id=request_context.organization.principal.trace_id,
            user_id=str(run.created_by),
        )
        async with self.session_factory() as session:
            return (
                await TenantService().get_workspace_access(
                    session,
                    principal=principal,
                    workspace_id=run.workspace_id,
                )
            ).context

    async def cancel(self, context: WorkspaceExecutionContext, *, run_id: UUID) -> AgentRunResult:
        self._require_permission(context, "agent_run")
        async with self.session_factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(AgentRun.workspace_id == UUID(context.workspace_id), AgentRun.id == run_id)
                .with_for_update()
            )
            if run is None:
                raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
            previous_status = run.status
            claimed_action = await session.scalar(
                select(Approval.id)
                .where(
                    Approval.workspace_id == run.workspace_id,
                    Approval.run_id == run.id,
                    Approval.execution_status.in_({"CLAIMED", "UNKNOWN_OUTCOME"}),
                )
                .limit(1)
            )
            if run.status == "NEEDS_ATTENTION":
                pass
            elif isinstance(claimed_action, UUID) and run.status in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            }:
                run.status = "NEEDS_ATTENTION"
                run.failure_code = "ACTION_RECONCILIATION_REQUIRED"
            elif isinstance(claimed_action, UUID):
                run.status = "CANCEL_REQUESTED"
            elif run.status == "WAITING_APPROVAL" and not run.execution_owner:
                run.status = "CANCELLED"
                run.completed_at = datetime.now(UTC)
            elif run.status in {"RUNNING", "WAITING_APPROVAL"}:
                run.status = "CANCEL_REQUESTED"
            if previous_status != run.status:
                run.event_log_complete = False
            await session.commit()
            result = _run_result(run)
        if result.status == "CANCELLED" and self.approval_service is not None:
            await self.approval_service.cancel_pending_for_run(context, run_id)
        return result

    async def _mark_waiting(self, context: WorkspaceExecutionContext, run_id: UUID) -> None:
        async with self.session_factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(AgentRun.workspace_id == UUID(context.workspace_id), AgentRun.id == run_id)
                .with_for_update()
            )
            if run is not None and run.status == "RUNNING":
                run.status = "WAITING_APPROVAL"
                run.event_log_complete = False
                await session.commit()

    async def _waiting_result(
        self,
        context: WorkspaceExecutionContext,
        run: AgentRun,
        *,
        model_step_count: int,
        tool_call_count: int,
        usage_records: list[dict[str, Any]],
        waiting_approval_id: UUID | None = None,
    ) -> AgentRunResult:
        async with self.session_factory() as session:
            persisted = await session.scalar(
                select(AgentRun)
                .where(AgentRun.workspace_id == UUID(context.workspace_id), AgentRun.id == run.id)
                .with_for_update()
            )
            if persisted is None:
                raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
            persisted.model_step_count = model_step_count
            persisted.tool_call_count = tool_call_count
            if persisted.status == "RUNNING":
                persisted.status = "WAITING_APPROVAL"
            persisted.event_log_complete = False
            persisted.waiting_approval_id = waiting_approval_id
            # A decision can commit while the graph is reaching its interrupt.
            # Recompute under the same Run lock used by the decision endpoint.
            decided = await session.scalar(
                select(Approval.id)
                .where(
                    Approval.workspace_id == persisted.workspace_id,
                    Approval.run_id == persisted.id,
                    Approval.decision_status != "PENDING",
                    Approval.execution_status == "NOT_STARTED",
                    *([] if waiting_approval_id is None else [Approval.id == waiting_approval_id]),
                )
                .limit(1)
            )
            persisted.resume_pending = isinstance(decided, UUID)
            for key, value in _aggregate_usage(usage_records).items():
                setattr(persisted, key, value)
            await session.commit()
            return AgentRunResult(
                run_id=persisted.id,
                agent_version_id=persisted.agent_version_id,
                status=persisted.status,
                final_output=None,
                failure_code=None,
                model_step_count=persisted.model_step_count,
                tool_call_count=persisted.tool_call_count,
                total_input_tokens=persisted.total_input_tokens,
                total_output_tokens=persisted.total_output_tokens,
                total_tokens=persisted.total_tokens,
                total_cached_tokens=persisted.total_cached_tokens,
                total_cost_amount=persisted.total_cost_amount,
                cost_currency=persisted.cost_currency,
            )

    async def open_stream(
        self,
        context: WorkspaceExecutionContext,
        *,
        agent_version_id: UUID,
        input_text: str,
        prepared_run: AgentRun | None = None,
        detached: bool = False,
    ) -> RunStreamHub:
        """Start a run and hand back the hub that owns it.

        The caller subscribes to the returned hub; it does not own the run.
        Dropping the subscription starts the hub's grace window instead of
        aborting, so a disconnected client can come back -- or a different one
        can take over -- without the work being thrown away.

        ``detached`` removes the grace window altogether, for a caller that is
        executing the run rather than watching it (the worker): there, having
        no subscribers is the normal state, not a sign of abandonment.
        """

        self._require_permission(context, "agent_run")
        if prepared_run is None:
            await self.preflight_stream(context, agent_version_id=agent_version_id)
            run = await self._create_run(context, agent_version_id, input_text)
        else:
            run = prepared_run
        existing_hub = self.stream_registry.get(run.id)
        if existing_hub is not None:
            return existing_hub
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue(maxsize=256)
        recorder = RunEventRecorder(
            self.session_factory, workspace_id=UUID(context.workspace_id), run_id=run.id
        )
        emitter = AgentEventEmitter(
            run_id=str(run.id),
            agent_version_id=str(agent_version_id),
            request_id=context.request_id,
            # Persist before the execution lease is released, rather than
            # waiting for the hub pump; resume may begin in another process.
            before_publish=recorder.arecord,
        )
        graph_holder: dict[str, _AgentRunGraph] = {}
        producer = asyncio.create_task(
            self._produce_stream(
                context,
                run,
                agent_version_id,
                queue,
                emitter,
                graph_holder,
            )
        )

        async def abort_if_active() -> None:
            if producer.done():
                if not producer.cancelled():
                    try:
                        producer.exception()
                    except Exception:
                        logger.warning("agent_stream_producer_failed", exc_info=True)
                return
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            graph = graph_holder.get("graph")
            await asyncio.shield(
                self._complete_run(
                    context,
                    run.id,
                    status="FAILED",
                    final_output=None,
                    failure_code="AGENT_STREAM_CANCELLED",
                    model_step_count=graph.model_round_count if graph else 0,
                    tool_call_count=graph.tool_call_count if graph else 0,
                    usage_records=graph.usage_records if graph else [],
                )
            )

        hub = RunStreamHub(
            run_id=run.id,
            source=queue,
            abort=abort_if_active,
            grace_seconds=None if detached else self.stream_grace_seconds,
            recorder=recorder,
            source_persisted=True,
            on_closed=self.stream_registry.discard,
        )
        self.stream_registry.register(hub)
        hub.start()
        return hub

    async def stream(
        self,
        context: WorkspaceExecutionContext,
        *,
        agent_version_id: UUID,
        input_text: str,
        prepared_run: AgentRun | None = None,
        graceful_disconnect: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Run the same LangGraph execution path while publishing AgentHub events.

        Direct callers receive deterministic cancellation when their consumer
        closes. HTTP routes opt into the hub's reconnect grace window because
        their SSE connection can be replaced by ``attach_stream``.
        """

        hub = await self.open_stream(
            context,
            agent_version_id=agent_version_id,
            input_text=input_text,
            prepared_run=prepared_run,
        )
        subscription = hub.subscribe()
        try:
            async for event in subscription:
                yield event
        finally:
            await subscription.aclose()
            if not graceful_disconnect:
                await hub.abort_if_unwatched()

    async def attach_stream(
        self,
        context: WorkspaceExecutionContext,
        *,
        run_id: UUID,
        after_sequence: int = 0,
        poll_seconds: float = 0.25,
    ) -> AsyncIterator[AgentEvent]:
        """Follow a run that this consumer did not start.

        Replays everything after ``after_sequence`` from the durable log, then
        keeps following: from the live hub when this process owns the run, and
        otherwise by tailing ``agent_run_events`` until the run reaches a
        terminal state. Tailing an ordered, durably-stored log is what lets a
        run be followed across processes without a second delivery path --
        the log already guarantees order and exactly-once.
        """

        self._require_permission(context, "agent_run")
        workspace_id = UUID(context.workspace_id)
        # Reuse the read path so a run in another workspace 404s rather than
        # revealing that the id exists.
        run = await self.get_run(context, run_id)
        agent_version_id = run.agent_version_id
        cursor = after_sequence

        async def replay() -> AsyncIterator[AgentEvent]:
            nonlocal cursor
            while True:
                events = await self.event_reader.read_after(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    agent_version_id=agent_version_id,
                    after_sequence=cursor,
                )
                if not events:
                    return
                for event in events:
                    cursor = event.sequence
                    yield event

        async for event in replay():
            yield event

        hub = self.stream_registry.get(run_id)
        if hub is not None:
            async for event in hub.subscribe():
                # Durable events may land between the initial replay and the
                # subscription. Never advance past that gap using a live cursor.
                if event.sequence > cursor + 1:
                    async for persisted in replay():
                        yield persisted
                if event.sequence <= cursor:
                    continue
                cursor = event.sequence
                yield event
            # The hub may have closed between the replay above and the
            # subscribe, so drain whatever landed in the gap.
            async for event in replay():
                yield event
            if hub.closed:
                return

        while True:
            if await self._run_stopped_publishing(context, run_id):
                async for event in replay():
                    yield event
                return
            await asyncio.sleep(poll_seconds)
            async for event in replay():
                yield event

    async def _run_stopped_publishing(
        self, context: WorkspaceExecutionContext, run_id: UUID
    ) -> bool:
        async with self.session_factory() as session:
            status = await session.scalar(
                select(AgentRun.status).where(
                    AgentRun.workspace_id == UUID(context.workspace_id),
                    AgentRun.id == run_id,
                )
            )
        return status in STREAM_FOLLOW_STOP_STATUSES

    @staticmethod
    def _initial_state(run: AgentRun, agent_version_id: UUID) -> AgentRunState:
        return {
            "run_id": str(run.id),
            "agent_version_id": str(agent_version_id),
            "messages": [],
            "pending_tool_calls": [],
            "proposed_tool_calls": [],
            "pre_observations": {},
            "tool_observations": [],
            "executed_observations": {},
            "model_round_count": 0,
            "tool_call_count": 0,
            "identical_call_counts": {},
            "final_output": None,
            "failure_code": None,
            "effective_knowledge_snapshots": list(run.effective_knowledge_snapshots or []),
            "effective_wiki_publications": list(run.effective_wiki_publications or []),
            "usage_records": [],
        }

    async def _produce_stream(
        self,
        context: WorkspaceExecutionContext,
        run: AgentRun,
        agent_version_id: UUID,
        queue: asyncio.Queue[AgentEvent | None],
        emitter: AgentEventEmitter,
        graph_holder: dict[str, _AgentRunGraph],
    ) -> None:
        """Run a producer and always close the consumer queue.

        A producer can fail before it has emitted a terminal event (including
        while serializing that event).  The queue close is deliberately
        non-blocking so a disconnected SSE client cannot leave the consumer
        waiting forever.
        """

        try:
            async with run_execution_lease(
                self.session_factory,
                UUID(context.workspace_id),
                run.id,
                allowed_statuses={"RUNNING", "CANCEL_REQUESTED"},
            ) as owner:
                if owner is None:
                    return
                await self._produce_stream_owned(
                    context, run, agent_version_id, queue, emitter, graph_holder
                )
        finally:
            _close_event_queue(queue)

    async def _produce_stream_owned(
        self, context, run, agent_version_id, queue, emitter, graph_holder
    ) -> None:
        try:
            await self._produce_stream_body(
                context, run, agent_version_id, queue, emitter, graph_holder
            )
        except asyncio.CancelledError:
            # Close out cancellation while ownership is still held. The lease
            # fences an unfinished graph conservatively; a known model-stream
            # close can be terminal, while _complete_run still gives claimed
            # or unknown actions precedence over ordinary cancellation.
            graph = graph_holder.get("graph")
            await asyncio.shield(
                self._complete_run(
                    context,
                    run.id,
                    status="FAILED",
                    final_output=None,
                    failure_code="AGENT_STREAM_CANCELLED",
                    model_step_count=graph.model_round_count if graph else 0,
                    tool_call_count=graph.tool_call_count if graph else 0,
                    usage_records=graph.usage_records if graph else [],
                )
            )
            raise
        except Exception:
            logger.warning("agent_stream_producer_failed", exc_info=True)
            graph = graph_holder.get("graph")
            try:
                await asyncio.shield(
                    self._complete_run(
                        context,
                        run.id,
                        status="FAILED",
                        final_output=None,
                        failure_code="AGENT_STREAM_PRODUCER_FAILED",
                        model_step_count=graph.model_round_count if graph else 0,
                        tool_call_count=graph.tool_call_count if graph else 0,
                        usage_records=graph.usage_records if graph else [],
                    )
                )
            except Exception:
                logger.warning("agent_stream_terminal_persistence_failed", exc_info=True)
            try:
                event = await emitter.emit(
                    AgentEventType.RUN_FAILED,
                    {
                        "status": "FAILED",
                        "failure_code": "AGENT_STREAM_PRODUCER_FAILED",
                        "model_step_count": graph.model_round_count if graph else 0,
                        "tool_call_count": graph.tool_call_count if graph else 0,
                    },
                )
                queue.put_nowait(event)
            except (Exception, asyncio.QueueFull):
                logger.warning("agent_stream_failure_event_failed", exc_info=True)
        finally:
            _close_event_queue(queue)

    async def _produce_stream_body(
        self,
        context: WorkspaceExecutionContext,
        run: AgentRun,
        agent_version_id: UUID,
        queue: asyncio.Queue[AgentEvent | None],
        emitter: AgentEventEmitter,
        graph_holder: dict[str, _AgentRunGraph],
    ) -> None:
        graph = _AgentRunGraph(
            self,
            context,
            run,
            mode="stream",
            emitter=emitter,
            event_queue=queue,
        )
        graph_holder["graph"] = graph
        state = self._initial_state(run, agent_version_id)
        final_state = state
        try:
            await _queue_event(
                queue,
                await emitter.emit(
                    AgentEventType.RUN_STARTED,
                    {"status": "RUNNING", "model_step_count": 0, "tool_call_count": 0},
                ),
            )
            final_state = await self._invoke_graph(graph, state, run)
            approval_payload = _approval_interrupt_payload(final_state)
            if approval_payload is not None:
                await self._mark_waiting(context, run.id)
                await self._waiting_result(
                    context,
                    run,
                    model_step_count=int(final_state.get("model_round_count", 0)),
                    tool_call_count=int(final_state.get("tool_call_count", 0)),
                    usage_records=final_state.get("usage_records", []),
                    waiting_approval_id=UUID(str(approval_payload["approval_id"])),
                )
                return
            failure_code = final_state.get("failure_code")
            status = final_state.get("run_status") or ("FAILED" if failure_code else "SUCCEEDED")
            final_output = final_state.get("final_output") if not failure_code else None
        except asyncio.CancelledError:
            await graph.close_active_model_stream()
            raise
        except AgentHubError as error:
            failure_code = error.code
            status = "FAILED"
            final_output = None
        except ModelGatewayError as error:
            failure_code = error.code.value
            status = "FAILED"
            final_output = None
        except Exception:
            logger.warning("agent_stream_failed", exc_info=True)
            failure_code = "AGENT_RUN_FAILED"
            status = "FAILED"
            final_output = None
        result = await self._complete_run(
            context,
            run.id,
            status=status,
            final_output=final_output,
            failure_code=failure_code,
            model_step_count=graph.model_round_count,
            tool_call_count=graph.tool_call_count,
            usage_records=graph.usage_records,
            turn_transcript=final_state.get("turn_transcript") if status == "SUCCEEDED" else None,
            turn_evidence_registry=(
                final_state.get("turn_evidence_registry") if status == "SUCCEEDED" else None
            ),
            pending_finish=graph.pending_finish,
        )
        if result.status == "CANCELLED":
            event_type = AgentEventType.RUN_CANCELLED
        else:
            event_type = (
                AgentEventType.RUN_COMPLETED
                if result.status == "SUCCEEDED"
                else AgentEventType.RUN_FAILED
            )
        data: dict[str, Any] = {
            "status": result.status,
            "model_step_count": result.model_step_count,
            "tool_call_count": result.tool_call_count,
        }
        if event_type is AgentEventType.RUN_FAILED:
            data["failure_code"] = result.failure_code or "AGENT_RUN_FAILED"
        elif event_type is AgentEventType.RUN_COMPLETED:
            data["output"] = result.final_output or ""
        await _queue_event(queue, await emitter.emit(event_type, data))

    async def get_run(self, context: WorkspaceExecutionContext, run_id: UUID) -> AgentRun:
        self._require_permission(context, "workspace_read")
        async with self.session_factory() as session:
            # A plain read. This is the read path -- the detail endpoint, the
            # step listing and every reconnecting stream follower go through it
            # -- and taking a row lock here makes readers queue behind the
            # worker that is still writing status, usage and output. Locking
            # belongs to the mutation paths that actually change the row.
            run = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == UUID(context.workspace_id), AgentRun.id == run_id
                )
            )
            if run is None:
                raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
            return run

    async def preflight_stream(
        self, context: WorkspaceExecutionContext, *, agent_version_id: UUID
    ) -> None:
        """Validate stream authorization and workspace scope before returning HTTP 200."""
        self._require_permission(context, "agent_run")
        try:
            workspace_id = UUID(context.workspace_id)
        except (TypeError, ValueError):
            raise AgentHubError(
                "AUTHENTICATION_REQUIRED", "Authentication is required.", 401
            ) from None
        async with self.session_factory() as session:
            version = await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.workspace_id == workspace_id,
                    AgentVersion.id == agent_version_id,
                )
            )
            if version is None:
                raise AgentHubError(
                    "AGENT_VERSION_NOT_FOUND", "The published agent version was not found.", 404
                )

    async def prepare_stream(
        self,
        context: WorkspaceExecutionContext,
        *,
        agent_version_id: UUID,
        input_text: str,
        thread_id: UUID | None = None,
        thread_turn_id: UUID | None = None,
    ) -> AgentRun:
        """Validate and persist the Run before the HTTP streaming response starts."""

        await self.preflight_stream(context, agent_version_id=agent_version_id)
        return await self._create_run(
            context,
            agent_version_id,
            input_text,
            thread_id=thread_id,
            thread_turn_id=thread_turn_id,
        )

    async def list_steps(self, context: WorkspaceExecutionContext, run_id: UUID) -> list[RunStep]:
        await self.get_run(context, run_id)
        async with self.session_factory() as session:
            result = await session.scalars(
                select(RunStep)
                .where(
                    RunStep.workspace_id == UUID(context.workspace_id),
                    RunStep.agent_run_id == run_id,
                )
                .order_by(RunStep.sequence_number)
            )
            return list(result)

    async def _create_run(
        self,
        context: WorkspaceExecutionContext,
        agent_version_id: UUID,
        input_text: str,
        *,
        execution_overrides: AgentRunExecutionOverrides | None = None,
        thread_id: UUID | None = None,
        thread_turn_id: UUID | None = None,
    ) -> AgentRun:
        try:
            workspace_id = UUID(context.workspace_id)
            created_by = UUID(str(context.user_id))
        except (TypeError, ValueError):
            raise AgentHubError(
                "AUTHENTICATION_REQUIRED", "Authentication is required.", 401
            ) from None
        if (thread_id is None) != (thread_turn_id is None):
            raise AgentHubError(
                "THREAD_TURN_BINDING_INVALID",
                "A thread turn and its thread must be supplied together.",
                422,
            )
        if thread_turn_id is not None and input_text is None:
            raise AgentHubError(
                "THREAD_TURN_BINDING_INVALID",
                "A thread-bound run requires input text.",
                422,
            )
        async with self.session_factory() as session:
            turn = None
            if thread_id is not None and thread_turn_id is not None:
                turn = await self._validate_thread_turn_binding(
                    session,
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    thread_turn_id=thread_turn_id,
                    input_text=input_text,
                )
            if turn is not None and thread_id is not None:
                if turn.agent_run_id is not None:
                    legacy_run = await session.scalar(
                        select(AgentRun).where(
                            AgentRun.workspace_id == workspace_id,
                            AgentRun.id == turn.agent_run_id,
                            AgentRun.thread_id == thread_id,
                            AgentRun.thread_turn_id == thread_turn_id,
                        )
                    )
                    if legacy_run is None:
                        raise AgentHubError(
                            "THREAD_TURN_BINDING_INVALID",
                            "The turn's existing run binding is inconsistent.",
                            409,
                        )
                    if (
                        legacy_run.agent_version_id != agent_version_id
                        or legacy_run.input_text != input_text
                    ):
                        raise AgentHubError(
                            "THREAD_TURN_BINDING_INVALID",
                            "The turn's existing run does not match the request.",
                            409,
                        )
                    return legacy_run
                existing = await self._load_run_for_turn(
                    session,
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    thread_turn_id=thread_turn_id,
                    agent_version_id=agent_version_id,
                    input_text=input_text,
                    turn_agent_run_id=getattr(turn, "agent_run_id", None),
                )
                if existing is not None:
                    return existing
            version = await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.workspace_id == workspace_id,
                    AgentVersion.id == agent_version_id,
                )
            )
            if version is None:
                raise AgentHubError(
                    "AGENT_VERSION_NOT_FOUND", "The published agent version was not found.", 404
                )
            if turn is not None and thread_id is not None:
                thread_agent_id = await session.scalar(
                    select(AgentThread.agent_id).where(
                        AgentThread.workspace_id == workspace_id,
                        AgentThread.id == thread_id,
                    )
                )
                if thread_agent_id != version.agent_id:
                    raise AgentHubError(
                        "THREAD_TURN_BINDING_INVALID",
                        "The agent version does not belong to the thread's agent.",
                        422,
                    )
            if canonical_json_hash(version.resolved_spec) != version.resolved_spec_hash:
                raise AgentHubError(
                    "AGENT_VERSION_INTEGRITY_ERROR",
                    "The published agent version is invalid.",
                    422,
                )
            published_hash = version.resolved_spec_hash
            try:
                spec = parse_frozen_agent_spec(version.resolved_spec, workspace_id=workspace_id)
                if version.spec_schema_version != version.resolved_spec.get("spec_schema_version"):
                    raise AgentHubError(
                        "AGENT_VERSION_INTEGRITY_ERROR",
                        "The published agent version schema is inconsistent.",
                        422,
                    )
            except AgentHubError as error:
                if error.code != "AGENT_VERSION_MODEL_BINDING_INVALID":
                    raise
                # Preserve durable failed-run history for malformed historical specs; the
                # prepare phase will return the same safe binding error without executing.
                effective_snapshots = []
                effective_wiki_publications = []
            else:
                if execution_overrides is None:
                    effective_snapshots = await self._resolve_effective_snapshots(
                        session, context, spec
                    )
                    effective_wiki_publications = await self._resolve_effective_wiki_publications(
                        session, context, spec
                    )
                else:
                    effective_snapshots = await self._validate_frozen_execution_snapshots(
                        session, context, spec, execution_overrides
                    )
                    effective_wiki_publications = await self._validate_frozen_execution_wiki(
                        session, context, spec, execution_overrides
                    )
            run = AgentRun(
                workspace_id=workspace_id,
                agent_version_id=agent_version_id,
                thread_id=thread_id,
                thread_turn_id=thread_turn_id,
                input_text=input_text,
                created_by=created_by,
                resolved_spec_hash=published_hash,
                effective_knowledge_snapshots=effective_snapshots,
                effective_wiki_publications=effective_wiki_publications,
            )
            turn_agent_run_id = turn.agent_run_id if turn is not None else None
            session.add(run)
            try:
                await session.commit()
            except IntegrityError as error:
                if turn is None or not self._is_thread_turn_unique_violation(error):
                    raise
                await session.rollback()
                winner = await self._load_run_for_turn(
                    session,
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    thread_turn_id=thread_turn_id,
                    agent_version_id=agent_version_id,
                    input_text=input_text,
                    turn_agent_run_id=turn_agent_run_id,
                )
                if winner is None:
                    raise
                return winner
            await session.refresh(run)
            return run

    async def _validate_thread_turn_binding(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        thread_turn_id: UUID,
        input_text: str,
    ) -> ThreadTurn:
        turn = await session.scalar(
            select(ThreadTurn)
            .where(
                ThreadTurn.workspace_id == workspace_id,
                ThreadTurn.id == thread_turn_id,
            )
            .with_for_update()
        )
        if (
            turn is None
            or turn.thread_id != thread_id
            or turn.user_input != input_text
            or turn.legacy_recovery_required
        ):
            raise AgentHubError(
                "THREAD_TURN_BINDING_INVALID",
                "The run input does not match the supplied thread turn.",
                422,
            )
        return turn

    async def _load_run_for_turn(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        thread_turn_id: UUID,
        agent_version_id: UUID,
        input_text: str,
        turn_agent_run_id: UUID | None = None,
    ) -> AgentRun | None:
        winner = await session.scalar(
            select(AgentRun).where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.thread_id == thread_id,
                AgentRun.thread_turn_id == thread_turn_id,
            )
        )
        if winner is None:
            return None
        if turn_agent_run_id is not None and turn_agent_run_id != winner.id:
            raise AgentHubError(
                "THREAD_TURN_BINDING_INVALID",
                "The turn and stable run bindings disagree.",
                409,
            )
        if winner.agent_version_id != agent_version_id or winner.input_text != input_text:
            raise AgentHubError(
                "THREAD_TURN_BINDING_INVALID",
                "The existing run does not match the supplied thread turn.",
                409,
            )
        return winner

    @staticmethod
    def _is_thread_turn_unique_violation(error: IntegrityError) -> bool:
        diag = getattr(error.orig, "diag", None)
        if getattr(diag, "constraint_name", None) == "uq_agent_runs_thread_turn":
            return True
        detail = str(diag or error)
        return "uq_agent_runs_thread_turn" in detail

    async def _validate_frozen_execution_snapshots(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        spec: FrozenAgentSpec,
        overrides: AgentRunExecutionOverrides,
    ) -> list[dict[str, str]]:
        bindings = list(spec.knowledge_bindings)
        snapshots = [dict(item) for item in overrides.effective_knowledge_snapshots]
        if len(bindings) != len(snapshots):
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "Frozen knowledge bindings do not match the published agent version.",
                409,
            )
        workspace_id = UUID(context.workspace_id)
        validated: list[dict[str, str]] = []
        for binding, frozen in zip(bindings, snapshots, strict=True):
            try:
                knowledge_base_id = UUID(str(frozen["knowledge_base_id"]))
                snapshot_id = UUID(str(frozen["snapshot_id"]))
                snapshot_hash = str(frozen.get("snapshot_hash") or frozen["snapshot_content_hash"])
            except (KeyError, TypeError, ValueError):
                raise AgentHubError(
                    "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                    "Frozen knowledge snapshot identity is invalid.",
                    409,
                ) from None
            if binding.knowledge_base_id != knowledge_base_id:
                raise AgentHubError(
                    "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                    "Frozen knowledge workspace binding is inconsistent.",
                    409,
                )
            if str(frozen.get("binding_mode", "")) != binding.binding_mode:
                raise AgentHubError(
                    "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                    "Frozen knowledge binding mode is inconsistent.",
                    409,
                )
            snapshot = await session.scalar(
                select(KnowledgeSnapshot).where(
                    KnowledgeSnapshot.workspace_id == workspace_id,
                    KnowledgeSnapshot.knowledge_base_id == knowledge_base_id,
                    KnowledgeSnapshot.id == snapshot_id,
                )
            )
            if snapshot is None or snapshot.content_hash != snapshot_hash:
                raise AgentHubError(
                    "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                    "The frozen knowledge snapshot is no longer valid.",
                    409,
                )
            if binding.binding_mode == "PINNED" and binding.snapshot_id != snapshot_id:
                raise AgentHubError(
                    "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                    "The pinned knowledge snapshot changed.",
                    409,
                )
            if binding.binding_mode == "PINNED" and binding.snapshot_hash != snapshot_hash:
                raise AgentHubError(
                    "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                    "The pinned knowledge snapshot hash changed.",
                    409,
                )
            validated.append(
                {
                    "knowledge_base_id": str(knowledge_base_id),
                    "snapshot_id": str(snapshot_id),
                    "snapshot_hash": snapshot_hash,
                }
            )
        return validated

    @staticmethod
    async def _resolve_effective_snapshots(
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        spec: FrozenAgentSpec,
    ) -> list[dict[str, str]]:
        if not spec.knowledge_bindings:
            return []
        run_context = context.model_copy(
            update={"permissions": context.permissions | frozenset({"knowledge_run"})}
        )
        service = KnowledgeSnapshotService()
        effective: list[dict[str, str]] = []
        for binding in spec.knowledge_bindings:
            if binding.binding_mode == "LATEST":
                if binding.knowledge_base_id is None:
                    raise AgentHubError(
                        "AGENT_VERSION_INTEGRITY_ERROR",
                        "The published knowledge binding is invalid.",
                        422,
                    )
                resolved = await service.resolve_snapshot(
                    session,
                    run_context,
                    binding.knowledge_base_id,
                    "LATEST",
                )
                effective.append(
                    {
                        "knowledge_base_id": str(binding.knowledge_base_id),
                        "snapshot_id": str(resolved.snapshot_id),
                        "snapshot_hash": resolved.content_hash,
                    }
                )
                continue
            if binding.snapshot_id is None or binding.snapshot_hash is None:
                raise AgentHubError(
                    "AGENT_VERSION_INTEGRITY_ERROR",
                    "The published knowledge binding is invalid.",
                    422,
                )
            if binding.knowledge_base_id is None:
                effective.append(
                    {
                        "snapshot_id": str(binding.snapshot_id),
                        "snapshot_hash": binding.snapshot_hash,
                    }
                )
                continue
            resolved = await service.resolve_snapshot(
                session,
                run_context,
                binding.knowledge_base_id,
                binding.snapshot_id,
            )
            if resolved.content_hash != binding.snapshot_hash:
                raise AgentHubError(
                    "AGENT_VERSION_INTEGRITY_ERROR",
                    "The published knowledge binding is invalid.",
                    422,
                )
            effective.append(
                {
                    "knowledge_base_id": str(binding.knowledge_base_id),
                    "snapshot_id": str(binding.snapshot_id),
                    "snapshot_hash": binding.snapshot_hash,
                }
            )
        return effective

    @staticmethod
    async def _resolve_effective_wiki_publications(
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        spec: FrozenAgentSpec,
    ) -> list[dict[str, str]]:
        """Freeze the version's Wiki binding onto a new run.

        The published binding is already resolved (publish froze LATEST), so
        this verifies the publication still exists under its frozen hash and
        carries no revoked source. A revoked publication refuses the new run
        outright instead of binding something the run cannot read.
        """

        binding = spec.wiki_binding
        if binding is None:
            return []
        wiki_context = context.model_copy(
            update={"permissions": context.permissions | frozenset({"knowledge_run"})}
        )
        publication = await session.scalar(
            select(WikiCompilation).where(
                WikiCompilation.workspace_id == UUID(context.workspace_id),
                WikiCompilation.knowledge_base_id == binding.knowledge_base_id,
                WikiCompilation.id == binding.wiki_publication_id,
            )
        )
        if (
            publication is None
            or publication.status != "PUBLISHED"
            or publication.content_hash != binding.publication_hash
        ):
            raise AgentHubError(
                "AGENT_VERSION_INTEGRITY_ERROR",
                "The published Wiki binding is invalid.",
                422,
            )
        await _assert_wiki_publication_not_revoked(session, wiki_context, publication)
        return [
            {
                "knowledge_base_id": str(binding.knowledge_base_id),
                "binding_mode": binding.binding_mode,
                "wiki_publication_id": str(binding.wiki_publication_id),
                "publication_hash": binding.publication_hash,
            }
        ]

    @staticmethod
    async def _validate_frozen_execution_wiki(
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        spec: FrozenAgentSpec,
        overrides: AgentRunExecutionOverrides,
    ) -> list[dict[str, str]]:
        """Replay-time validation of the evaluation worker's frozen publications."""

        binding = spec.wiki_binding
        frozen = [dict(item) for item in overrides.effective_wiki_publications]
        if binding is None:
            if frozen:
                raise AgentHubError(
                    "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                    "Frozen Wiki bindings do not match the published agent version.",
                    409,
                )
            return []
        if len(frozen) != 1:
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "Frozen Wiki bindings do not match the published agent version.",
                409,
            )
        item = frozen[0]
        workspace_id = UUID(context.workspace_id)
        try:
            knowledge_base_id = UUID(str(item["knowledge_base_id"]))
            publication_id = UUID(str(item["wiki_publication_id"]))
            publication_hash = str(item["publication_hash"])
            binding_mode = str(item["binding_mode"])
        except (KeyError, TypeError, ValueError):
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "Frozen Wiki publication identity is invalid.",
                409,
            ) from None
        if (
            knowledge_base_id != binding.knowledge_base_id
            or publication_id != binding.wiki_publication_id
            or publication_hash != binding.publication_hash
            or binding_mode != binding.binding_mode
        ):
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "Frozen Wiki publication does not match the published binding.",
                409,
            )
        publication = await session.scalar(
            select(WikiCompilation).where(
                WikiCompilation.workspace_id == workspace_id,
                WikiCompilation.knowledge_base_id == knowledge_base_id,
                WikiCompilation.id == publication_id,
            )
        )
        if (
            publication is None
            or publication.status != "PUBLISHED"
            or publication.content_hash != publication_hash
        ):
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "The frozen Wiki publication is no longer valid.",
                409,
            )
        wiki_context = context.model_copy(
            update={"permissions": context.permissions | frozenset({"knowledge_run"})}
        )
        await _assert_wiki_publication_not_revoked(session, wiki_context, publication)
        return [
            {
                "knowledge_base_id": str(knowledge_base_id),
                "binding_mode": binding_mode,
                "wiki_publication_id": str(publication_id),
                "publication_hash": publication_hash,
            }
        ]

    async def _frozen_wiki_publications_intact(self, run: AgentRun) -> bool:
        """Integrity-only replay check: each frozen publication still resolves.

        Source revocation after the run was created is historical semantics and
        keeps the run going; a publication that no longer matches its frozen
        hash means the run's binding cannot be honoured at all, which is a
        NEEDS_ATTENTION condition rather than something to replace silently.
        """

        publications = list(run.effective_wiki_publications or [])
        if not publications:
            return True
        async with self.session_factory() as session:
            for item in publications:
                try:
                    knowledge_base_id = UUID(str(item["knowledge_base_id"]))
                    publication_id = UUID(str(item["wiki_publication_id"]))
                    publication_hash = str(item["publication_hash"])
                except (KeyError, TypeError, ValueError):
                    return False
                publication = await session.scalar(
                    select(WikiCompilation).where(
                        WikiCompilation.workspace_id == run.workspace_id,
                        WikiCompilation.knowledge_base_id == knowledge_base_id,
                        WikiCompilation.id == publication_id,
                    )
                )
                if (
                    publication is None
                    or publication.status != "PUBLISHED"
                    or publication.content_hash != publication_hash
                ):
                    return False
        return True

    async def _append_step(
        self,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        sequence_number: int,
        *,
        kind: str,
        status: str,
        safe_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                RunStep(
                    workspace_id=UUID(context.workspace_id),
                    agent_run_id=run_id,
                    sequence_number=sequence_number,
                    kind=kind,
                    status=status,
                    safe_metadata=dict(safe_metadata or {}),
                )
            )
            await session.commit()

    async def _complete_run(
        self,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        *,
        status: str,
        final_output: str | None,
        failure_code: str | None,
        model_step_count: int,
        tool_call_count: int,
        usage_records: list[dict[str, Any]],
        turn_transcript: list[Any] | None = None,
        turn_evidence_registry: dict[str, Any] | None = None,
        pending_finish: Mapping[str, Any] | None = None,
    ) -> AgentRunResult:
        async with self.session_factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(AgentRun.workspace_id == UUID(context.workspace_id), AgentRun.id == run_id)
                .with_for_update()
            )
            if run is None:
                raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
            effective_status = status
            effective_output = final_output
            effective_failure = failure_code
            claim = await session.scalar(
                select(Approval.id)
                .where(
                    Approval.workspace_id == run.workspace_id,
                    Approval.run_id == run.id,
                    Approval.execution_status.in_({"CLAIMED", "UNKNOWN_OUTCOME"}),
                )
                .limit(1)
            )
            uncertain = (
                status == "NEEDS_ATTENTION"
                or _is_uncertain_action_failure(failure_code)
                or isinstance(claim, UUID)
            )
            if uncertain:
                effective_status = "NEEDS_ATTENTION"
                effective_output = None
                effective_failure = failure_code or "ACTION_RECONCILIATION_REQUIRED"
            elif run.status in {"CANCELLED", "NEEDS_ATTENTION"}:
                effective_status = run.status
                effective_output = run.final_output
                effective_failure = run.failure_code
            elif run.status == "CANCEL_REQUESTED":
                if status == "NEEDS_ATTENTION" or _is_uncertain_action_failure(failure_code):
                    effective_status = "NEEDS_ATTENTION"
                    effective_output = None
                    effective_failure = failure_code or "ACTION_RECONCILIATION_REQUIRED"
                else:
                    effective_status = "CANCELLED"
                    effective_output = None
                    effective_failure = None
            run.status = effective_status
            run.event_log_complete = False
            run.resume_pending = False
            run.waiting_approval_id = None
            run.final_output = effective_output
            run.failure_code = effective_failure
            previous_rounds = getattr(run, "model_step_count", 0) or 0
            run.model_step_count = max(model_step_count, previous_rounds)
            run.tool_call_count = max(tool_call_count, getattr(run, "tool_call_count", 0) or 0)
            if (
                effective_status == "SUCCEEDED"
                and turn_transcript is not None
                and run.thread_turn_id
            ):
                try:
                    transcript = build_turn_transcript(
                        # asyncpg may return UUID subclasses; normalize at the
                        # persistence boundary to the transcript's exact type.
                        run_id=UUID(str(run.id)),
                        turn_id=UUID(str(run.thread_turn_id)),
                        messages=turn_transcript,
                    )
                    if (
                        transcript.messages[0].content != run.input_text
                        or transcript.messages[-1].content != effective_output
                        or sum(len(message.tool_calls) for message in transcript.messages)
                        != run.tool_call_count
                    ):
                        raise ValueError("turn transcript does not match run")
                    payload = transcript.to_payload()
                except (TypeError, ValueError):
                    # The business outcome is already determined; leave the
                    # source unavailable rather than inventing a transcript.
                    logger.warning("thread_history_transcript_unavailable")
                else:
                    if (
                        run.thread_history_transcript is not None
                        and run.thread_history_transcript != payload
                    ):
                        raise AgentHubError(
                            "AGENT_HISTORY_SNAPSHOT_INVALID",
                            "The completed turn transcript changed.",
                            409,
                        )
                    run.thread_history_transcript = payload
                    try:
                        evidence = visible_evidence_registry(
                            transcript.messages, turn_evidence_registry or {}
                        )
                        if (
                            len(
                                json.dumps(evidence, ensure_ascii=False, allow_nan=False).encode(
                                    "utf-8"
                                )
                            )
                            > MAX_ENCODED_BYTES
                        ):
                            raise ValueError("thread evidence exceeds the storage bound")
                    except (AgentHubError, TypeError, ValueError):
                        logger.warning("thread_history_evidence_unavailable")
                    else:
                        if (
                            run.thread_history_evidence_registry is not None
                            and run.thread_history_evidence_registry != evidence
                        ):
                            raise AgentHubError(
                                "AGENT_HISTORY_SNAPSHOT_INVALID",
                                "The completed turn evidence changed.",
                                409,
                            )
                        run.thread_history_evidence_registry = evidence
            # Abort before restoring the checkpoint has no new usage evidence;
            # it must not erase spend already persisted by an earlier segment.
            if usage_records or previous_rounds == 0:
                for key, value in _aggregate_usage(usage_records).items():
                    setattr(run, key, value)
            existing_finish = (
                await session.scalar(
                    select(RunStep)
                    .where(
                        RunStep.workspace_id == run.workspace_id,
                        RunStep.agent_run_id == run.id,
                        RunStep.kind == "FINISH",
                    )
                    .order_by(RunStep.sequence_number)
                    .limit(1)
                )
                if pending_finish is not None
                else None
            )
            if pending_finish is not None and existing_finish is None:
                latest_sequence = await session.scalar(
                    select(func.max(RunStep.sequence_number)).where(
                        RunStep.workspace_id == run.workspace_id,
                        RunStep.agent_run_id == run.id,
                    )
                )
                safe_metadata = pending_finish.get("safe_metadata")
                if not isinstance(safe_metadata, Mapping):
                    safe_metadata = {}
                session.add(
                    RunStep(
                        workspace_id=run.workspace_id,
                        agent_run_id=run.id,
                        sequence_number=int(latest_sequence or 0) + 1,
                        kind="FINISH",
                        status=effective_status,
                        safe_metadata={
                            **dict(safe_metadata),
                            "status": effective_status,
                            "error_code": effective_failure,
                        },
                    )
                )
            run.completed_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(run)
            await self._enqueue_memory_extraction(context, run)
            return AgentRunResult(
                run_id=run.id,
                agent_version_id=run.agent_version_id,
                status=run.status,
                final_output=run.final_output,
                failure_code=run.failure_code,
                model_step_count=run.model_step_count,
                tool_call_count=run.tool_call_count,
                total_input_tokens=run.total_input_tokens,
                total_output_tokens=run.total_output_tokens,
                total_tokens=run.total_tokens,
                total_cached_tokens=run.total_cached_tokens,
                total_cost_amount=run.total_cost_amount,
                cost_currency=run.cost_currency,
            )

    async def _enqueue_memory_extraction(
        self, context: WorkspaceExecutionContext, run: AgentRun
    ) -> None:
        """The one place every execution path finishes a run.

        ``ThreadService.submit_turn`` runs in process, ``/runs/stream`` may hand
        off to a worker, and a resumed approval takes a third route; the only
        thing all of them share is that they end here, after the status is
        committed. Enqueueing anywhere else would silently skip a path.

        Only successful thread turns are worth extracting from: a Playground
        run has no thread to remember for, and a failed run has no answer. The
        published version's ``long_term_memory`` switch is deliberately *not*
        checked here -- the worker re-checks it against the database, so a
        stale or replayed message cannot make an agent learn something it was
        not configured to learn. Nothing in this method may fail a run that has
        already succeeded, hence the bare except.
        """

        queue = self.memory_writer
        if queue is None or run.thread_id is None or run.status != "SUCCEEDED":
            return
        try:
            await queue.enqueue(
                workspace_id=run.workspace_id,
                run_id=run.id,
                request_id=context.request_id,
            )
        except Exception:
            logger.warning("memory_extraction_enqueue_failed", exc_info=True)

    @staticmethod
    def _require_permission(context: WorkspaceExecutionContext, permission: str) -> None:
        if permission not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
