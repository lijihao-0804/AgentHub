"""Running a published MCP tool, on both sides of the approval boundary.

This is the seam between AgentHub's runtime and a remote server. Everything
that has to touch the database or a credential to make a remote call happen
lives here, so ``ToolRuntime`` and ``ActionRuntime`` stay what they are —
orchestration — and do not grow SQL or ciphers.

The two entry points differ only in what an unanswered call means. A READ that
cannot be completed is simply an error: nothing happened out there, and if
something did, reading it again costs nothing. A WRITE that cannot be confirmed
is never called a failure, because the remote may have acted; it is handed back
as indeterminate and a human is asked to reconcile it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.config.settings import Settings, get_settings
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.mcp.client import (
    DispatchState,
    McpCallOutcome,
    McpCallStatus,
    McpClientAdapter,
    McpConnectionTarget,
)
from packages.mcp.models import McpAuthType, McpConnection
from packages.mcp.security import McpSecretCipher, McpSecretError
from packages.tools.actions import ActionExecutionResult
from packages.tools.contracts import ToolDefinition, ToolExecutionContext, ToolSourceKind
from packages.tools.errors import ToolHandlerError

MCP_CONNECTION_NOT_FOUND = "MCP_CONNECTION_NOT_FOUND"
MCP_CONNECTION_DISABLED = "MCP_CONNECTION_DISABLED"
MCP_AUTH_NOT_CONFIGURED = "MCP_AUTH_NOT_CONFIGURED"
MCP_SECRET_DECRYPTION_FAILED = "MCP_SECRET_DECRYPTION_FAILED"
MCP_TOOL_NOT_PUBLISHED = "MCP_TOOL_NOT_PUBLISHED"


class McpToolExecutor:
    """Calls one published remote tool on behalf of a workspace."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings | None = None,
        adapter: McpClientAdapter | None = None,
        cipher: McpSecretCipher | None = None,
    ) -> None:
        self.session_factory = session_factory
        self._settings = settings
        self._adapter = adapter
        self._cipher = cipher

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    @property
    def adapter(self) -> McpClientAdapter:
        if self._adapter is None:
            self._adapter = McpClientAdapter(settings=self.settings)
        return self._adapter

    @property
    def cipher(self) -> McpSecretCipher:
        if self._cipher is None:
            self._cipher = McpSecretCipher.from_settings(self.settings)
        return self._cipher

    async def execute_read(
        self,
        context: ToolExecutionContext,
        definition: ToolDefinition,
        arguments: Mapping[str, Any],
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> dict[str, Any]:
        """Run a READ tool. Shaped as an ordinary tool handler on purpose.

        Arguments have already been checked against the frozen input schema and
        ToolPolicy has already allowed the call; by the time control reaches
        here the only question left is what the server says.
        """

        del session_factory
        outcome = await self._call(context.workspace_context, definition, arguments)
        if outcome.status is McpCallStatus.OK:
            return outcome.data or {}
        # A READ never comes back as "we are not sure". Reading again is free,
        # so an unconfirmed read is just an error the agent can react to.
        raise ToolHandlerError(
            outcome.failure_code or "MCP_TOOL_CALL_FAILED",
            "The remote MCP tool call did not succeed.",
        )

    async def execute_write(
        self,
        context: WorkspaceExecutionContext,
        definition: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> ActionExecutionResult:
        """Dispatch an approved WRITE tool once and classify the answer.

        AgentHub intentionally dispatches the call a single time and never
        retries an uncertain outcome. That is a statement about what this side
        does, not a promise about the remote: no exactly-once guarantee is
        claimed or available here.
        """

        outcome = await self._call(context, definition, arguments)
        if outcome.status is McpCallStatus.OK:
            return ActionExecutionResult.succeeded(outcome.data or {})
        if outcome.status is McpCallStatus.TOOL_ERROR:
            # The server said the tool failed. That is a definite answer about a
            # completed round trip, so it is a failure, not a mystery.
            return ActionExecutionResult.failed(
                outcome.failure_code or "MCP_TOOL_CALL_FAILED",
                "The remote MCP tool reported a failure.",
            )
        if outcome.dispatch is DispatchState.NOT_DISPATCHED:
            return ActionExecutionResult.failed(
                outcome.failure_code or "MCP_TOOL_CALL_FAILED",
                "The remote MCP tool was not called.",
            )
        return ActionExecutionResult.unknown_outcome(
            outcome.failure_code or "MCP_TOOL_CALL_FAILED"
        )

    async def _call(
        self,
        context: WorkspaceExecutionContext,
        definition: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> McpCallOutcome:
        """Resolve the connection, then make exactly one remote call.

        Every refusal before the adapter is reached is reported as
        NOT_DISPATCHED, which is what makes it safe to fail a WRITE outright:
        no request existed to be half-delivered.
        """

        if definition.source_kind is not ToolSourceKind.MCP or not definition.mcp_tool_name:
            return _not_dispatched(MCP_TOOL_NOT_PUBLISHED)
        try:
            workspace_id = UUID(str(context.workspace_id))
        except (TypeError, ValueError):
            return _not_dispatched(MCP_CONNECTION_NOT_FOUND)
        try:
            target, secret = await self._outbound(workspace_id, definition.mcp_connection_id)
        except _ResolutionRefused as refusal:
            return _not_dispatched(refusal.failure_code)
        return await self.adapter.call_tool(
            target,
            secret,
            tool_name=definition.mcp_tool_name,
            arguments=arguments,
            timeout_seconds=definition.timeout_seconds,
            max_result_bytes=self.settings.mcp_tool_result_max_bytes,
            output_schema=definition.mcp_output_schema,
        )

    async def _outbound(
        self, workspace_id: UUID, connection_id: UUID | None
    ) -> tuple[McpConnectionTarget, str | None]:
        """Read the connection this run may use, and decrypt its token now.

        The credential is read at the moment of the call rather than frozen at
        publish, so rotating a token takes effect without republishing and a
        revoked one stops working immediately. The contract is immutable; the
        credential is not, and that asymmetry is deliberate.
        """

        if connection_id is None:
            raise _ResolutionRefused(MCP_CONNECTION_NOT_FOUND)
        async with self.session_factory() as session:
            record = (
                await session.execute(
                    select(
                        McpConnection.id,
                        McpConnection.endpoint_url,
                        McpConnection.auth_type,
                        McpConnection.enabled,
                        McpConnection.secret_ciphertext,
                    ).where(
                        McpConnection.workspace_id == workspace_id,
                        McpConnection.id == connection_id,
                    )
                )
            ).first()
        if record is None:
            raise _ResolutionRefused(MCP_CONNECTION_NOT_FOUND)
        if not record.enabled:
            # Switching a connection off has to stop runs that were published
            # while it was on, or the switch means nothing.
            raise _ResolutionRefused(MCP_CONNECTION_DISABLED)
        auth_type = (
            record.auth_type
            if isinstance(record.auth_type, McpAuthType)
            else McpAuthType(str(record.auth_type))
        )
        secret: str | None = None
        if auth_type is McpAuthType.BEARER:
            if record.secret_ciphertext is None:
                raise _ResolutionRefused(MCP_AUTH_NOT_CONFIGURED)
            try:
                secret = self.cipher.decrypt(record.secret_ciphertext)
            except McpSecretError:
                raise _ResolutionRefused(MCP_SECRET_DECRYPTION_FAILED) from None
        return (
            McpConnectionTarget(
                connection_id=record.id, endpoint_url=record.endpoint_url, auth_type=auth_type
            ),
            secret,
        )


class McpActionExecutor:
    """One executor for every remote WRITE tool there will ever be.

    Remote tools are data, not code: registering a Python callable per imported
    identity would mean the action registry had to be mutated every time a
    workspace imported something. Dispatch is by source instead, and the
    definition carries which remote tool to call.
    """

    def __init__(self, executor: McpToolExecutor) -> None:
        self.executor = executor

    async def execute(
        self,
        context: WorkspaceExecutionContext,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ActionExecutionResult:
        # The key correlates this attempt in AgentHub's own audit. It is
        # deliberately not passed to the server: the frozen input schema is a
        # closed object, and inventing an argument would both break it and
        # imply a de-duplication guarantee no remote server has promised.
        del idempotency_key
        return await self.executor.execute_write(context, definition, arguments)


class _ResolutionRefused(Exception):
    def __init__(self, failure_code: str) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code


def _not_dispatched(failure_code: str) -> McpCallOutcome:
    return McpCallOutcome(
        McpCallStatus.FAILED, DispatchState.NOT_DISPATCHED, failure_code=failure_code
    )


__all__ = [
    "MCP_AUTH_NOT_CONFIGURED",
    "MCP_CONNECTION_DISABLED",
    "MCP_CONNECTION_NOT_FOUND",
    "MCP_SECRET_DECRYPTION_FAILED",
    "MCP_TOOL_NOT_PUBLISHED",
    "McpActionExecutor",
    "McpToolExecutor",
]
