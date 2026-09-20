"""Provider-neutral WRITE action execution boundary for M5."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.tools.contracts import ToolDefinition, ToolSourceKind
from packages.tools.models import Customer, Ticket


class ActionExecutionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


@dataclass(frozen=True)
class ActionExecutionResult:
    status: ActionExecutionStatus
    data: dict[str, Any] | None = None
    failure_code: str | None = None
    safe_message: str | None = None

    @classmethod
    def succeeded(cls, data: dict[str, Any]) -> ActionExecutionResult:
        return cls(ActionExecutionStatus.SUCCEEDED, data=data)

    @classmethod
    def failed(cls, code: str, message: str) -> ActionExecutionResult:
        return cls(ActionExecutionStatus.FAILED, failure_code=code, safe_message=message)

    @classmethod
    def unknown_outcome(cls, code: str = "ACTION_OUTCOME_UNKNOWN") -> ActionExecutionResult:
        return cls(
            ActionExecutionStatus.UNKNOWN_OUTCOME,
            failure_code=code,
            safe_message="The action outcome requires reconciliation.",
        )


class ActionExecutor(Protocol):
    async def execute(
        self,
        context: WorkspaceExecutionContext,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ActionExecutionResult: ...


class CreateTicketActionExecutor:
    """Internal transactional WRITE action with effectively-once semantics."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def execute(
        self,
        context: WorkspaceExecutionContext,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ActionExecutionResult:
        del definition
        try:
            workspace_id = UUID(context.workspace_id)
        except (TypeError, ValueError):
            return ActionExecutionResult.failed("INVALID_TOOL_CONTEXT", "The workspace is invalid.")
        customer_ref = arguments.get("customer_ref")
        subject = arguments.get("subject")
        priority = arguments.get("priority", "MEDIUM")
        if not isinstance(customer_ref, str) or not isinstance(subject, str):
            return ActionExecutionResult.failed(
                "TOOL_ARGUMENT_INVALID", "The create_ticket arguments are invalid."
            )
        async with self.session_factory() as session:
            existing = await _find_ticket(session, workspace_id, idempotency_key)
            if existing is not None:
                return _ticket_result(existing)
            customer = await session.scalar(
                select(Customer).where(
                    Customer.workspace_id == workspace_id, Customer.customer_ref == customer_ref
                )
            )
            if customer is None:
                return ActionExecutionResult.failed(
                    "CUSTOMER_NOT_FOUND", "The customer was not found in this workspace."
                )
            ticket = Ticket(
                workspace_id=workspace_id,
                customer_id=customer.id,
                ticket_ref=f"T-{uuid4().hex[:16].upper()}",
                subject=subject,
                idempotency_key=idempotency_key,
                metadata_json={"priority": priority},
            )
            session.add(ticket)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await _find_ticket(session, workspace_id, idempotency_key)
                if existing is not None:
                    return _ticket_result(existing)
                return ActionExecutionResult.failed(
                    "TICKET_CREATE_CONFLICT", "The ticket could not be created safely."
                )
            await session.refresh(ticket)
            return _ticket_result(ticket)


class ActionRegistry:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        overrides: dict[str, ActionExecutor] | None = None,
        mcp_executor: ActionExecutor | None = None,
    ) -> None:
        self._executors: dict[str, ActionExecutor] = {
            "create_ticket": CreateTicketActionExecutor(session_factory)
        }
        if overrides:
            self._executors.update(overrides)
        self._mcp_executor = mcp_executor

    def resolve(self, identity: str) -> ActionExecutor | None:
        return self._executors.get(identity)

    def resolve_for(self, definition: ToolDefinition) -> ActionExecutor | None:
        """Find who runs this action, by source first and identity second.

        Remote tools do not each get an entry here. There is one executor for
        all of them, because a remote tool is a row in a table rather than a
        piece of code, and a registry that grew an entry per import would be a
        registry a workspace could write into.
        """

        if definition.source_kind is ToolSourceKind.MCP:
            return self._mcp_executor
        return self.resolve(definition.identity)


class ActionRuntime:
    """Executes only an already-approved action claim."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ActionRegistry | None = None,
        mcp_executor: ActionExecutor | None = None,
    ) -> None:
        self.registry = registry or ActionRegistry(
            session_factory=session_factory, mcp_executor=mcp_executor
        )

    async def execute(
        self,
        context: WorkspaceExecutionContext,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ActionExecutionResult:
        if definition.effect.value != "WRITE":
            return ActionExecutionResult.failed(
                "ACTION_NOT_WRITE", "Only WRITE tools can use the action runtime."
            )
        executor = self.registry.resolve_for(definition)
        if executor is None:
            return ActionExecutionResult.failed("UNKNOWN_ACTION", "The action is not registered.")
        if definition.source_kind is ToolSourceKind.MCP:
            return await self._execute_remote(
                executor, context, definition, arguments, idempotency_key
            )
        try:
            async with asyncio.timeout(definition.timeout_seconds):
                return await executor.execute(
                    context,
                    definition,
                    arguments,
                    idempotency_key=idempotency_key,
                )
        except TimeoutError:
            # Safe for a local action: the work runs in this process, so
            # cancelling it is the same as it not having happened.
            return ActionExecutionResult.failed("ACTION_TIMEOUT", "The action timed out.")
        except AgentHubError as exc:
            return ActionExecutionResult.failed(exc.code, exc.message)
        except Exception:
            return ActionExecutionResult.failed("ACTION_EXECUTION_FAILED", "The action failed.")

    @staticmethod
    async def _execute_remote(
        executor: ActionExecutor,
        context: WorkspaceExecutionContext,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> ActionExecutionResult:
        """Run a remote action, without imposing a deadline from out here.

        Cutting a remote call off from this layer would be a lie: the request
        may already be with the other side, and cancelling our end tells us
        nothing about theirs. Only the client that handed the bytes over knows
        whether the attempt got that far, so it owns the deadline and reports
        the doubt. Anything that still escapes is treated as doubt too — the
        safe answer to "did it happen?" is "ask a human", never "no".
        """

        try:
            return await executor.execute(
                context, definition, arguments, idempotency_key=idempotency_key
            )
        except Exception:
            return ActionExecutionResult.unknown_outcome("ACTION_OUTCOME_UNKNOWN")


async def _find_ticket(
    session: AsyncSession, workspace_id: UUID, idempotency_key: str
) -> Ticket | None:
    return await session.scalar(
        select(Ticket).where(
            Ticket.workspace_id == workspace_id, Ticket.idempotency_key == idempotency_key
        )
    )


def _ticket_result(ticket: Ticket) -> ActionExecutionResult:
    return ActionExecutionResult.succeeded(
        {
            "ticket_id": str(ticket.id),
            "ticket_ref": ticket.ticket_ref,
            "status": ticket.status,
        }
    )


__all__ = [
    "ActionExecutionResult",
    "ActionExecutionStatus",
    "ActionExecutor",
    "ActionRegistry",
    "ActionRuntime",
    "CreateTicketActionExecutor",
]
