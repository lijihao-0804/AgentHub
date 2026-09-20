"""Workspace-scoped management of remote MCP connections.

This layer owns authorization, workspace scoping, secret handling and the
lifecycle rules; it never speaks MCP. Everything protocol-shaped is delegated
to :class:`packages.mcp.client.McpClientAdapter`, which hands back normalized
values and stable failure codes.
"""

from __future__ import annotations

from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.rbac import WORKSPACE_ADMIN, WORKSPACE_READ
from packages.core.config.settings import Settings, get_settings
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.mcp.client import (
    McpClientAdapter,
    McpConnectionTarget,
    McpRemoteError,
)
from packages.mcp.models import McpAuthType, McpConnection
from packages.mcp.security import (
    McpSecretCipher,
    McpSecretError,
    McpTargetError,
    parse_endpoint_url,
)

# The existing workspace-operational permission. A developer who may edit tools
# may verify and inspect a connection an administrator approved; only an
# administrator may create one or touch its credential. No new permission is
# introduced by 3A.
TOOL_EDIT = "tool_edit"

MAX_NAME_LENGTH = 128

NOT_FOUND = "MCP_CONNECTION_NOT_FOUND"
NOT_FOUND_MESSAGE = "The MCP connection was not found."


def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
    try:
        return UUID(context.workspace_id)
    except (TypeError, ValueError):
        raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None


def _principal_id(context: WorkspaceExecutionContext) -> UUID:
    try:
        if context.user_id is None:
            raise ValueError
        return UUID(context.user_id)
    except (TypeError, ValueError):
        raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401) from None


def _require(context: WorkspaceExecutionContext, permission: str) -> UUID:
    if permission not in context.permissions:
        raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
    return _workspace_id(context)


def _not_found() -> NoReturn:
    # The same answer whether the id belongs to another workspace or to nobody:
    # a 404 that varied would turn this endpoint into an existence oracle.
    raise AgentHubError(NOT_FOUND, NOT_FOUND_MESSAGE, 404)


def _auth_type(value: str) -> McpAuthType:
    try:
        return McpAuthType(value)
    except ValueError:
        raise AgentHubError(
            "MCP_AUTH_TYPE_INVALID", "The auth type is not supported.", 422
        ) from None


def _validated_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        raise AgentHubError("MCP_CONNECTION_INVALID", "The connection name is invalid.", 422)
    return name


def _validated_endpoint(value: str) -> str:
    try:
        return parse_endpoint_url(value).url
    except McpTargetError:
        raise AgentHubError(
            "MCP_ENDPOINT_INVALID",
            "The endpoint must be an absolute http or https URL without credentials.",
            422,
        ) from None


class McpConnectionService:
    """Management operations with explicit workspace and permission guards."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        adapter: McpClientAdapter | None = None,
        cipher: McpSecretCipher | None = None,
    ) -> None:
        self._settings = settings
        self._adapter = adapter
        self._cipher = cipher

    # -- dependencies ------------------------------------------------------

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
            try:
                self._cipher = McpSecretCipher.from_settings(self.settings)
            except McpSecretError as exc:
                raise AgentHubError(
                    "MCP_SECRET_ENCRYPTION_UNAVAILABLE",
                    "MCP secret encryption is not configured.",
                    500,
                ) from exc
        return self._cipher

    # -- reads -------------------------------------------------------------

    async def list_connections(
        self, session: AsyncSession, context: WorkspaceExecutionContext
    ) -> list[dict[str, Any]]:
        workspace_id = _require(context, WORKSPACE_READ)
        result = await session.execute(
            self._safe_projection()
            .where(McpConnection.workspace_id == workspace_id)
            .order_by(McpConnection.created_at, McpConnection.id)
        )
        return [dict(row._mapping) for row in result]

    async def get_connection(
        self, session: AsyncSession, context: WorkspaceExecutionContext, connection_id: UUID
    ) -> dict[str, Any]:
        workspace_id = _require(context, WORKSPACE_READ)
        row = await self._safe_row(session, workspace_id, connection_id)
        if row is None:
            _not_found()
        return row

    # -- writes ------------------------------------------------------------

    async def create_connection(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        *,
        name: str,
        endpoint_url: str,
        auth_type: str,
        secret: str | None,
        enabled: bool,
    ) -> dict[str, Any]:
        workspace_id = _require(context, WORKSPACE_ADMIN)
        created_by = _principal_id(context)
        resolved_auth = _auth_type(auth_type)
        ciphertext = self._secret_for(resolved_auth, secret)

        connection = McpConnection(
            workspace_id=workspace_id,
            name=_validated_name(name),
            endpoint_url=_validated_endpoint(endpoint_url),
            auth_type=resolved_auth.value,
            secret_ciphertext=ciphertext,
            secret_version=1 if ciphertext is not None else None,
            enabled=enabled,
            created_by=created_by,
        )
        session.add(connection)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "MCP_CONNECTION_CREATE_FAILED",
                "The MCP connection could not be created.",
                409,
            ) from exc
        return await self._view(session, workspace_id, connection.id)

    async def patch_connection(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        connection_id: UUID,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """Rename or enable/disable. The endpoint and auth type are settled.

        A connection is the identity of one remote server under one credential.
        Letting either be edited in place would let everything already pointing
        at it be redirected without leaving a trace, which is precisely the
        property 3B's publish freezing will need to rely on. Pointing at a
        different server means creating a different connection.
        """

        workspace_id = _require(context, WORKSPACE_ADMIN)
        # Settled before the row is read: an attempt to repoint a connection is
        # refused outright rather than after a partial application.
        if set(values) - {"name", "enabled"}:
            raise AgentHubError(
                "MCP_CONNECTION_IMMUTABLE",
                "Only the name and enabled flag can be changed.",
                422,
            )
        connection = await self._locked(session, workspace_id, connection_id)
        for key, value in values.items():
            setattr(connection, key, _validated_name(value) if key == "name" else value)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "MCP_CONNECTION_UPDATE_FAILED",
                "The MCP connection could not be updated.",
                409,
            ) from exc
        return await self._view(session, workspace_id, connection_id)

    async def rotate_secret(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        connection_id: UUID,
        secret: str,
    ) -> dict[str, Any]:
        workspace_id = _require(context, WORKSPACE_ADMIN)
        connection = await self._locked(session, workspace_id, connection_id)
        if connection.auth_type != McpAuthType.BEARER.value:
            raise AgentHubError(
                "MCP_AUTH_NOT_CONFIGURED",
                "This connection does not authenticate with a bearer token.",
                409,
            )
        connection.secret_ciphertext = self._encrypt(secret)
        connection.secret_version = (connection.secret_version or 0) + 1
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "MCP_CONNECTION_ROTATE_FAILED",
                "The MCP connection secret could not be rotated.",
                409,
            ) from exc
        return await self._view(session, workspace_id, connection_id)

    # -- remote operations -------------------------------------------------

    async def test_connection(
        self, session: AsyncSession, context: WorkspaceExecutionContext, connection_id: UUID
    ) -> dict[str, Any]:
        """Handshake with the remote and report its health. Runs no tool."""

        workspace_id = _require(context, TOOL_EDIT)
        target, secret = await self._outbound(session, workspace_id, connection_id)
        outcome = await self.adapter.test_connection(target, secret)
        return {
            "connection_id": connection_id,
            "status": outcome.status,
            "failure_code": outcome.failure.failure_code if outcome.failure is not None else None,
            "latency_ms": outcome.latency_ms,
            "protocol_version": outcome.server.protocol_version if outcome.server else None,
            "server_name": outcome.server.server_name if outcome.server else None,
        }

    async def discover_tools(
        self, session: AsyncSession, context: WorkspaceExecutionContext, connection_id: UUID
    ) -> dict[str, Any]:
        """Read the remote's tool catalog. Imports nothing and persists nothing.

        The result describes only what the server declared. Nothing here is an
        AgentHub Tool, and no effect, risk level, approval policy or execution
        kind is inferred: those are governance decisions taken later, against a
        catalog a human has looked at.
        """

        workspace_id = _require(context, TOOL_EDIT)
        target, secret = await self._outbound(session, workspace_id, connection_id)
        try:
            outcome = await self.adapter.discover_tools(target, secret)
        except McpRemoteError as exc:
            # A failure must not look like "this server has no tools".
            raise AgentHubError(
                exc.failure_code, "Tool discovery against the remote MCP server failed.", 502
            ) from None
        return {
            "connection_id": connection_id,
            "protocol_version": outcome.server.protocol_version if outcome.server else None,
            "server_name": outcome.server.server_name if outcome.server else None,
            "tools": [
                {
                    "name": entry.name,
                    "title": entry.title,
                    "description": entry.description,
                    "input_schema": entry.input_schema,
                    "output_schema": entry.output_schema,
                    "remote_annotations": entry.remote_annotations,
                }
                for entry in outcome.tools
            ],
        }

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _safe_projection():
        """The only SELECT the response path may use.

        Listing the columns explicitly, and deriving ``secret_configured`` as a
        boolean in SQL, means the ciphertext is never loaded into an object a
        serializer could reach. The guarantee is structural rather than a rule
        about what to remember to strip.
        """

        return select(
            McpConnection.id,
            McpConnection.workspace_id,
            McpConnection.name,
            McpConnection.endpoint_url,
            McpConnection.auth_type,
            McpConnection.enabled,
            McpConnection.created_by,
            McpConnection.created_at,
            McpConnection.updated_at,
            McpConnection.secret_ciphertext.is_not(None).label("secret_configured"),
        )

    async def _view(
        self, session: AsyncSession, workspace_id: UUID, connection_id: UUID
    ) -> dict[str, Any]:
        """Re-read a row a write has just authorized.

        Deliberately not routed through :meth:`get_connection`: an operation
        already cleared for administration must not additionally require the
        read permission, or a write could succeed and then report a 403.
        """

        row = await self._safe_row(session, workspace_id, connection_id)
        if row is None:  # pragma: no cover - the row was just written
            _not_found()
        return row

    async def _safe_row(
        self, session: AsyncSession, workspace_id: UUID, connection_id: UUID
    ) -> dict[str, Any] | None:
        result = await session.execute(
            self._safe_projection().where(
                McpConnection.workspace_id == workspace_id,
                McpConnection.id == connection_id,
            )
        )
        row = result.first()
        return dict(row._mapping) if row is not None else None

    async def _locked(
        self, session: AsyncSession, workspace_id: UUID, connection_id: UUID
    ) -> McpConnection:
        connection = await session.scalar(
            select(McpConnection)
            .where(
                McpConnection.id == connection_id,
                McpConnection.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if connection is None:
            _not_found()
        return connection

    async def _outbound(
        self, session: AsyncSession, workspace_id: UUID, connection_id: UUID
    ) -> tuple[McpConnectionTarget, str | None]:
        """Resolve a connection into what one outbound call needs, and no more."""

        row = await session.execute(
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
        record = row.first()
        if record is None:
            _not_found()
        if not record.enabled:
            # Disabled means disabled: no packet leaves for this endpoint.
            raise AgentHubError(
                "MCP_CONNECTION_DISABLED", "The MCP connection is disabled.", 409
            )
        auth_type = _auth_type(record.auth_type)
        secret = None
        if auth_type is McpAuthType.BEARER:
            if record.secret_ciphertext is None:
                raise AgentHubError(
                    "MCP_AUTH_NOT_CONFIGURED",
                    "This connection has no bearer token configured.",
                    409,
                )
            secret = self._decrypt(record.secret_ciphertext)
        target = McpConnectionTarget(
            connection_id=record.id, endpoint_url=record.endpoint_url, auth_type=auth_type
        )
        return target, secret

    def _secret_for(self, auth_type: McpAuthType, secret: str | None) -> str | None:
        supplied = secret if secret is not None and secret.strip() else None
        if auth_type is McpAuthType.NONE:
            if supplied is not None:
                raise AgentHubError(
                    "MCP_AUTH_INVALID",
                    "A secret cannot be supplied when the auth type is NONE.",
                    422,
                )
            return None
        if supplied is None:
            raise AgentHubError(
                "MCP_AUTH_INVALID", "A bearer connection requires a non-empty secret.", 422
            )
        return self._encrypt(supplied)

    def _encrypt(self, secret: str) -> str:
        value = secret.strip() if isinstance(secret, str) else secret
        if not value:
            raise AgentHubError(
                "MCP_AUTH_INVALID", "A bearer connection requires a non-empty secret.", 422
            )
        try:
            return self.cipher.encrypt(value)
        except McpSecretError as exc:
            raise AgentHubError(
                "MCP_SECRET_ENCRYPTION_FAILED", "The MCP secret could not be encrypted.", 409
            ) from exc

    def _decrypt(self, ciphertext: str) -> str:
        try:
            return self.cipher.decrypt(ciphertext)
        except McpSecretError as exc:
            raise AgentHubError(
                "MCP_SECRET_DECRYPTION_FAILED", "The MCP secret could not be decrypted.", 409
            ) from exc


__all__ = ["TOOL_EDIT", "McpConnectionService"]
