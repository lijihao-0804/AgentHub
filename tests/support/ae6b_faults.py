"""Bounded crash injection for the AE-6b test worker.

The injector replaces only production methods at their durable return
boundaries. It is installed in the test worker process from explicit
environment configuration and writes identity-only evidence before exiting.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import select

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime_service import AgentRunService
from packages.approvals.contracts import ApprovalExecutionStatus
from packages.approvals.models import Approval
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.interactive_lifecycle import (
    CaseExecutionIdentity,
    InteractiveLifecycle,
)
from packages.evaluation.models import (
    EvaluationExperimentCaseResult,
    InteractiveEpisodeTurn,
)
from packages.tools.actions import (
    ActionExecutionResult,
    ActionExecutionStatus,
    CreateTicketActionExecutor,
)
from packages.tools.contracts import ToolDefinition

FAULT_POINT_ENV = "AGENTHUB_AE6B_FAULT_POINT"
FAULT_PATH_ENV = "AGENTHUB_AE6B_FAULT_PATH"
FAULT_EXIT_CODE = 73

FIXTURE_COMMITTED = "fixture_committed"
FIRST_RUN_TERMINAL = "first_run_terminal"
WRITE_RESPONSE_LOST = "write_response_lost"
OBSERVATION_RETAINED = "observation_retained"
FAULT_POINTS = (
    FIXTURE_COMMITTED,
    FIRST_RUN_TERMINAL,
    WRITE_RESPONSE_LOST,
    OBSERVATION_RETAINED,
)


@dataclass(frozen=True, slots=True)
class _FaultIdentity:
    workspace_id: UUID
    run_id: UUID
    case_id: UUID
    agent_run_id: UUID | None


@dataclass(frozen=True, slots=True)
class _RunFaultState:
    identity: _FaultIdentity
    turn_index: int
    agent_status: str
    first_turn_dispatch_status: str | None
    next_turn_bound: bool
    unknown_action_retained: bool


class AE6BFaultInjector:
    """Install idempotent, test-only crash wrappers for one configured point."""

    _install_lock = threading.Lock()
    _fault_lock = threading.Lock()
    _installed = False
    _point: str | None = None
    _fault_path: Path | None = None
    _write_succeeded = False
    _write_workspace_id: UUID | None = None

    @classmethod
    def install_from_env(cls) -> bool:
        point = (os.environ.get(FAULT_POINT_ENV) or "").strip()
        path_text = (os.environ.get(FAULT_PATH_ENV) or "").strip()
        if not point and not path_text:
            return False
        if not point or not path_text:
            raise RuntimeError(
                "AE6B fault injection requires both AGENTHUB_AE6B_FAULT_POINT "
                "and AGENTHUB_AE6B_FAULT_PATH"
            )
        return cls.install(point, Path(path_text))

    @classmethod
    def install(cls, point: str, fault_path: Path) -> bool:
        if point not in FAULT_POINTS:
            raise RuntimeError(f"AE6B_FAULT_POINT_UNSUPPORTED:{point}")
        normalized_path = fault_path.expanduser()
        with cls._install_lock:
            if cls._installed:
                if cls._point == point and cls._fault_path == normalized_path:
                    return True
                raise RuntimeError("AE6B_FAULT_INJECTOR_ALREADY_INSTALLED")
            cls._point = point
            cls._fault_path = normalized_path
            if point == FIXTURE_COMMITTED:
                cls._install_lifecycle_point(
                    method_name="create_fixture",
                    point=point,
                )
            elif point == FIRST_RUN_TERMINAL or point == WRITE_RESPONSE_LOST:
                cls._install_agent_run_point("execute_prepared_run")
                if point == WRITE_RESPONSE_LOST:
                    cls._install_agent_run_point("resume")
                    cls._install_write_response_loss()
            elif point == OBSERVATION_RETAINED:
                cls._install_lifecycle_point(
                    method_name="retain_observation",
                    point=point,
                )
            cls._installed = True
            return True

    @classmethod
    def _install_lifecycle_point(cls, *, method_name: str, point: str) -> None:
        marker = f"_ae6b_fault_{method_name}_installed"
        if getattr(InteractiveLifecycle, marker, False):
            return
        original = getattr(InteractiveLifecycle, method_name)

        async def wrapper(
            lifecycle: InteractiveLifecycle,
            identity: CaseExecutionIdentity,
            **kwargs: Any,
        ):
            result = await original(lifecycle, identity, **kwargs)
            agent_run_id = (
                await cls._first_turn_agent_run_id(
                    lifecycle,
                    workspace_id=identity.workspace_id,
                    case_id=identity.case_id,
                )
                if method_name == "retain_observation"
                else None
            )
            cls._write_fault_and_exit(
                point=point,
                workspace_id=identity.workspace_id,
                run_id=identity.run_id,
                case_id=identity.case_id,
                agent_run_id=agent_run_id,
            )
            return result

        setattr(InteractiveLifecycle, method_name, wrapper)
        setattr(InteractiveLifecycle, marker, True)

    @classmethod
    def _install_agent_run_point(cls, method_name: str) -> None:
        marker = f"_ae6b_fault_{method_name}_installed"
        if getattr(AgentRunService, marker, False):
            return
        original = getattr(AgentRunService, method_name)

        async def wrapper(
            service: AgentRunService,
            context: WorkspaceExecutionContext,
            **kwargs: Any,
        ):
            result = await original(service, context, **kwargs)
            await cls._inspect_agent_run_result(
                service,
                context,
                run_id=result.run_id,
                status=result.status,
            )
            return result

        setattr(AgentRunService, method_name, wrapper)
        setattr(AgentRunService, marker, True)

    @classmethod
    def _install_write_response_loss(cls) -> None:
        marker = "_ae6b_fault_write_response_loss_installed"
        if getattr(CreateTicketActionExecutor, marker, False):
            return
        original = CreateTicketActionExecutor.execute

        async def wrapper(
            executor: CreateTicketActionExecutor,
            context: WorkspaceExecutionContext,
            definition: ToolDefinition,
            arguments: dict[str, Any],
            *,
            idempotency_key: str,
        ) -> ActionExecutionResult:
            result = await original(
                executor,
                context,
                definition,
                arguments,
                idempotency_key=idempotency_key,
            )
            if result.status is ActionExecutionStatus.SUCCEEDED:
                cls._write_succeeded = True
                cls._write_workspace_id = UUID(str(context.workspace_id))
                return ActionExecutionResult.unknown_outcome()
            return result

        CreateTicketActionExecutor.execute = wrapper
        setattr(CreateTicketActionExecutor, marker, True)

    @classmethod
    async def _inspect_agent_run_result(
        cls,
        service: AgentRunService,
        context: WorkspaceExecutionContext,
        *,
        run_id: UUID,
        status: str,
    ) -> None:
        if cls._point == FIRST_RUN_TERMINAL:
            if status != "SUCCEEDED":
                return
            state = await cls._load_run_fault_state(
                service,
                workspace_id=UUID(str(context.workspace_id)),
                agent_run_id=run_id,
            )
            if (
                state is None
                or state.turn_index != 0
                or state.agent_status != "SUCCEEDED"
                or state.first_turn_dispatch_status != "DISPATCH_INTENT"
                or state.next_turn_bound
            ):
                return
            cls._write_fault_and_exit(
                point=FIRST_RUN_TERMINAL,
                workspace_id=state.identity.workspace_id,
                run_id=state.identity.run_id,
                case_id=state.identity.case_id,
                agent_run_id=state.identity.agent_run_id,
            )

        if cls._point != WRITE_RESPONSE_LOST or not cls._write_succeeded:
            return
        workspace_id = UUID(str(context.workspace_id))
        if workspace_id != cls._write_workspace_id or status != "NEEDS_ATTENTION":
            return
        state = await cls._load_run_fault_state(
            service,
            workspace_id=workspace_id,
            agent_run_id=run_id,
        )
        if (
            state is None
            or state.agent_status != "NEEDS_ATTENTION"
            or not state.unknown_action_retained
        ):
            return
        cls._write_fault_and_exit(
            point=WRITE_RESPONSE_LOST,
            workspace_id=state.identity.workspace_id,
            run_id=state.identity.run_id,
            case_id=state.identity.case_id,
            agent_run_id=state.identity.agent_run_id,
        )

    @staticmethod
    async def _first_turn_agent_run_id(
        lifecycle: InteractiveLifecycle,
        *,
        workspace_id: UUID,
        case_id: UUID,
    ) -> UUID | None:
        async with lifecycle.session_factory() as session:
            return await session.scalar(
                select(InteractiveEpisodeTurn.agent_run_id)
                .where(
                    InteractiveEpisodeTurn.workspace_id == workspace_id,
                    InteractiveEpisodeTurn.case_id == case_id,
                )
                .order_by(InteractiveEpisodeTurn.turn_index)
                .limit(1)
            )

    @classmethod
    async def _load_run_fault_state(
        cls,
        service: AgentRunService,
        *,
        workspace_id: UUID,
        agent_run_id: UUID,
    ) -> _RunFaultState | None:
        async with service.session_factory() as session:
            turn = await session.scalar(
                select(InteractiveEpisodeTurn).where(
                    InteractiveEpisodeTurn.workspace_id == workspace_id,
                    InteractiveEpisodeTurn.agent_run_id == agent_run_id,
                )
            )
            if turn is None:
                return None
            case = await session.scalar(
                select(EvaluationExperimentCaseResult).where(
                    EvaluationExperimentCaseResult.workspace_id == workspace_id,
                    EvaluationExperimentCaseResult.id == turn.case_id,
                )
            )
            run = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.id == agent_run_id,
                )
            )
            turns = list(
                await session.scalars(
                    select(InteractiveEpisodeTurn)
                    .where(
                        InteractiveEpisodeTurn.workspace_id == workspace_id,
                        InteractiveEpisodeTurn.case_id == turn.case_id,
                    )
                    .order_by(InteractiveEpisodeTurn.turn_index)
                )
            )
            unknown_approval_id = await session.scalar(
                select(Approval.id)
                .where(
                    Approval.workspace_id == workspace_id,
                    Approval.run_id == agent_run_id,
                    Approval.execution_status == ApprovalExecutionStatus.UNKNOWN_OUTCOME,
                )
                .limit(1)
            )
        if case is None or run is None:
            return None
        first_turn = next((item for item in turns if item.turn_index == 0), None)
        return _RunFaultState(
            identity=_FaultIdentity(
                workspace_id=workspace_id,
                run_id=case.experiment_run_id,
                case_id=case.id,
                agent_run_id=agent_run_id,
            ),
            turn_index=turn.turn_index,
            agent_status=run.status,
            first_turn_dispatch_status=(
                first_turn.dispatch_status if first_turn is not None else None
            ),
            next_turn_bound=any(
                item.turn_index > 0
                and (item.thread_turn_id is not None or item.agent_run_id is not None)
                for item in turns
            ),
            unknown_action_retained=unknown_approval_id is not None,
        )

    @classmethod
    def _write_fault_and_exit(
        cls,
        *,
        point: str,
        workspace_id: UUID,
        run_id: UUID,
        case_id: UUID,
        agent_run_id: UUID | None,
    ) -> NoReturn:
        if cls._fault_path is None:
            raise RuntimeError("AE6B_FAULT_PATH_NOT_CONFIGURED")
        record = {
            "point": point,
            "workspace_id": str(workspace_id),
            "run_id": str(run_id),
            "case_id": str(case_id),
            "agent_run_id": str(agent_run_id) if agent_run_id is not None else None,
            "pid": os.getpid(),
        }
        payload = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        cls._fault_path.parent.mkdir(parents=True, exist_ok=True)
        with cls._fault_lock:
            with cls._fault_path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        os._exit(FAULT_EXIT_CODE)


def install_configured_fault() -> bool:
    """Install the one fault configured for this test worker, if any."""

    return AE6BFaultInjector.install_from_env()


__all__ = [
    "AE6BFaultInjector",
    "FAULT_EXIT_CODE",
    "FAULT_PATH_ENV",
    "FAULT_POINT_ENV",
    "FAULT_POINTS",
    "FIRST_RUN_TERMINAL",
    "FIXTURE_COMMITTED",
    "OBSERVATION_RETAINED",
    "WRITE_RESPONSE_LOST",
    "install_configured_fault",
]
