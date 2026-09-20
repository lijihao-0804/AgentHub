"""Workspace-scoped product control-plane services.

The services in this module are management projections over existing M2--M4
persistence.  They deliberately do not participate in runtime resolution or
publication semantics.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.models import (
    Agent,
    AgentKnowledgeBinding,
    AgentTool,
    Tool,
    ToolRevision,
)
from packages.agent_runtime.tool_revisions import validate_tool_spec
from packages.control_plane.rbac import WORKSPACE_ADMIN
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.models import (
    Document,
    DocumentRevision,
    KnowledgeBase,
    KnowledgeSnapshot,
    KnowledgeSnapshotItem,
)
from packages.model_gateway.capabilities import capabilities_from_mapping
from packages.model_gateway.credentials import CredentialEncryptionError
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.tools.validation import validate_executable_tool_spec

_SUPPORTED_LITELLM_PROVIDERS = frozenset({"deepseek", "openai-compatible", "openai"})

# This is the server-owned catalog.  The three read tools are backed by
# ToolRegistry; create_ticket is the existing ActionRuntime builtin action.
BUILTIN_TOOL_CATALOG: dict[str, dict[str, Any]] = {
    "calculator": {
        "description": "Evaluate bounded arithmetic expressions.",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
            "additionalProperties": False,
        },
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "timeout_seconds": 30,
        "execution_kind": "builtin",
    },
    "query_customer": {
        "description": "Read a customer and optionally its open tickets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_ref": {"type": "string"},
                "include_open_tickets": {"type": "boolean"},
            },
            "required": ["customer_ref"],
            "additionalProperties": False,
        },
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "timeout_seconds": 30,
        "execution_kind": "builtin",
    },
    "search_knowledge": {
        "description": "Search a published knowledge snapshot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "timeout_seconds": 30,
        "execution_kind": "builtin",
    },
    "create_ticket": {
        "description": "Create a support ticket after the existing approval boundary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_ref": {"type": "string"},
                "subject": {"type": "string"},
                "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
            },
            "required": ["customer_ref", "subject"],
            "additionalProperties": False,
        },
        "effect": "WRITE",
        "risk_level": "HIGH",
        "approval_policy": "ALWAYS",
        "timeout_seconds": 30,
        "execution_kind": "action",
    },
}


class BuiltinToolCatalog:
    """Read-only server catalog for the executable builtin identities."""

    @classmethod
    def identities(cls) -> tuple[str, ...]:
        return tuple(BUILTIN_TOOL_CATALOG)

    @classmethod
    def get(cls, identity: str) -> dict[str, Any]:
        return _catalog_entry(identity)

    @classmethod
    def spec(cls, identity: str) -> dict[str, Any]:
        return _catalog_spec(identity)


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


def _not_found(code: str, message: str) -> NoReturn:
    raise AgentHubError(code, message, 404)


def _provider_name(value: str) -> str:
    provider = value.strip().lower().replace("_", "-")
    if provider not in _SUPPORTED_LITELLM_PROVIDERS:
        raise AgentHubError("INVALID_PROVIDER", "The model provider is not supported.", 422)
    return provider


def _catalog_entry(identity: str) -> dict[str, Any]:
    entry = BUILTIN_TOOL_CATALOG.get(identity)
    if entry is None:
        raise AgentHubError("TOOL_CATALOG_NOT_FOUND", "The builtin tool is not available.", 422)
    return {"identity": identity, **deepcopy(entry)}


def _catalog_spec(identity: str) -> dict[str, Any]:
    entry = _catalog_entry(identity)
    return {
        "kind": "builtin",
        "identity": identity,
        "description": entry["description"],
        "input_schema": entry["input_schema"],
        "effect": entry["effect"],
        "risk_level": entry["risk_level"],
        "approval_policy": entry["approval_policy"],
        "timeout_seconds": entry["timeout_seconds"],
    }


def _safe_spec(revision: ToolRevision) -> dict[str, Any]:
    try:
        raw_spec = validate_tool_spec(revision.spec)
        if canonical_json_hash(raw_spec) != revision.spec_hash:
            raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
        spec = validate_executable_tool_spec(raw_spec)
    except (AgentHubError, TypeError, ValueError) as exc:
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422) from exc
    identity = spec.get("identity")
    if identity not in BUILTIN_TOOL_CATALOG:
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
    return spec


class ProductControlPlaneService:
    """Management operations with explicit workspace and permission guards."""

    async def list_provider_credentials(
        self, session: AsyncSession, context: WorkspaceExecutionContext
    ) -> list[dict[str, Any]]:
        workspace_id = _require(context, "workspace_read")
        result = await session.execute(
            select(
                ProviderCredential.id,
                ProviderCredential.workspace_id,
                ProviderCredential.provider,
                ProviderCredential.name,
                ProviderCredential.base_url,
                ProviderCredential.enabled,
                ProviderCredential.created_at,
            )
            .where(ProviderCredential.workspace_id == workspace_id)
            .order_by(ProviderCredential.created_at, ProviderCredential.id)
        )
        return [dict(row._mapping) for row in result]

    async def get_provider_credential(
        self, session: AsyncSession, context: WorkspaceExecutionContext, credential_id: UUID
    ) -> dict[str, Any]:
        workspace_id = _require(context, "workspace_read")
        row = await self._credential_row(session, workspace_id, credential_id)
        if row is None:
            _not_found("PROVIDER_CREDENTIAL_NOT_FOUND", "The provider credential was not found.")
        return row

    async def create_provider_credential(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        *,
        provider: str,
        name: str,
        secret: str,
        base_url: str | None,
        enabled: bool,
    ) -> dict[str, Any]:
        workspace_id = _require(context, WORKSPACE_ADMIN)
        provider = _provider_name(provider)
        credential = ProviderCredential(
            workspace_id=workspace_id,
            provider=provider,
            name=name.strip(),
            secret=secret,
            base_url=base_url,
            enabled=enabled,
        )
        session.add(credential)
        try:
            await session.commit()
        except (CredentialEncryptionError, IntegrityError) as exc:
            await session.rollback()
            code = (
                "CREDENTIAL_ENCRYPTION_FAILED"
                if isinstance(exc, CredentialEncryptionError)
                else "PROVIDER_CREDENTIAL_CREATE_FAILED"
            )
            message = (
                "The provider credential could not be encrypted."
                if isinstance(exc, CredentialEncryptionError)
                else "The provider credential could not be created."
            )
            raise AgentHubError(code, message, 409) from exc
        return await self.get_provider_credential(session, context, credential.id)

    async def patch_provider_credential(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        credential_id: UUID,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        workspace_id = _require(context, WORKSPACE_ADMIN)
        credential = await session.scalar(
            select(ProviderCredential)
            .where(
                ProviderCredential.id == credential_id,
                ProviderCredential.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if credential is None:
            _not_found("PROVIDER_CREDENTIAL_NOT_FOUND", "The provider credential was not found.")
        for key, value in values.items():
            setattr(credential, key, value.strip() if key == "name" else value)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "PROVIDER_CREDENTIAL_UPDATE_FAILED",
                "The provider credential could not be updated.",
                409,
            ) from exc
        return await self.get_provider_credential(session, context, credential_id)

    async def rotate_provider_secret(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        credential_id: UUID,
        secret: str,
    ) -> dict[str, Any]:
        workspace_id = _require(context, WORKSPACE_ADMIN)
        credential = await session.scalar(
            select(ProviderCredential)
            .where(
                ProviderCredential.id == credential_id,
                ProviderCredential.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if credential is None:
            _not_found("PROVIDER_CREDENTIAL_NOT_FOUND", "The provider credential was not found.")
        credential.secret = secret
        try:
            await session.commit()
        except (CredentialEncryptionError, IntegrityError) as exc:
            await session.rollback()
            code = (
                "CREDENTIAL_ENCRYPTION_FAILED"
                if isinstance(exc, CredentialEncryptionError)
                else "PROVIDER_CREDENTIAL_ROTATE_FAILED"
            )
            message = (
                "The provider credential could not be encrypted."
                if isinstance(exc, CredentialEncryptionError)
                else "The provider credential secret could not be rotated."
            )
            raise AgentHubError(code, message, 409) from exc
        return await self.get_provider_credential(session, context, credential_id)

    async def list_model_profiles(
        self, session: AsyncSession, context: WorkspaceExecutionContext
    ) -> list[ModelProfile]:
        workspace_id = _require(context, "workspace_read")
        result = await session.scalars(
            select(ModelProfile)
            .where(ModelProfile.workspace_id == workspace_id)
            .order_by(ModelProfile.created_at, ModelProfile.id)
        )
        return list(result)

    async def get_model_profile(
        self, session: AsyncSession, context: WorkspaceExecutionContext, profile_id: UUID
    ) -> ModelProfile:
        workspace_id = _require(context, "workspace_read")
        profile = await session.scalar(
            select(ModelProfile).where(
                ModelProfile.id == profile_id, ModelProfile.workspace_id == workspace_id
            )
        )
        if profile is None:
            _not_found("MODEL_PROFILE_NOT_FOUND", "The model profile was not found.")
        return profile

    async def create_model_profile(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        values: dict[str, Any],
    ) -> ModelProfile:
        workspace_id = _require(context, "agent_edit")
        await self._validate_profile_values(session, workspace_id, None, values)
        profile = ModelProfile(workspace_id=workspace_id, **values)
        session.add(profile)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "MODEL_PROFILE_CREATE_FAILED", "The model profile could not be created.", 409
            ) from exc
        return profile

    async def patch_model_profile(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        profile_id: UUID,
        values: dict[str, Any],
    ) -> ModelProfile:
        workspace_id = _require(context, "agent_edit")
        profile = await session.scalar(
            select(ModelProfile)
            .where(ModelProfile.id == profile_id, ModelProfile.workspace_id == workspace_id)
            .with_for_update()
        )
        if profile is None:
            _not_found("MODEL_PROFILE_NOT_FOUND", "The model profile was not found.")
        proposed = {
            "provider_credential_id": profile.provider_credential_id,
            "model": profile.model,
            "temperature": profile.temperature,
            "max_tokens": profile.max_tokens,
            "timeout_seconds": profile.timeout_seconds,
            "fallback_profile_id": profile.fallback_profile_id,
            "capabilities": profile.capabilities,
            "enabled": profile.enabled,
        }
        proposed.update(values)
        await self._validate_profile_values(session, workspace_id, profile_id, proposed)
        for key, value in values.items():
            setattr(profile, key, value)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "MODEL_PROFILE_UPDATE_FAILED", "The model profile could not be updated.", 409
            ) from exc
        return profile

    async def _validate_profile_values(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        profile_id: UUID | None,
        values: dict[str, Any],
    ) -> None:
        credential = await session.scalar(
            select(ProviderCredential.id).where(
                ProviderCredential.id == values["provider_credential_id"],
                ProviderCredential.workspace_id == workspace_id,
            )
        )
        if credential is None:
            _not_found("PROVIDER_CREDENTIAL_NOT_FOUND", "The provider credential was not found.")
        if not isinstance(values.get("model"), str) or not values["model"].strip():
            raise AgentHubError("INVALID_MODEL_PROFILE", "The model profile is invalid.", 422)
        try:
            capabilities_from_mapping(values.get("capabilities", {}))
            if Decimal(str(values["temperature"])) < 0:
                raise ValueError
            if int(values["max_tokens"]) <= 0 or Decimal(str(values["timeout_seconds"])) <= 0:
                raise ValueError
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise AgentHubError(
                "INVALID_MODEL_PROFILE", "The model profile is invalid.", 422
            ) from exc
        fallback = values.get("fallback_profile_id")
        if fallback is None:
            return
        if profile_id is not None and fallback == profile_id:
            raise AgentHubError(
                "MODEL_PROFILE_FALLBACK_CYCLE", "Fallback profiles must not cycle.", 422
            )
        visited: set[UUID] = {profile_id} if profile_id is not None else set()
        current: UUID | None = fallback
        while current is not None:
            if current in visited:
                raise AgentHubError(
                    "MODEL_PROFILE_FALLBACK_CYCLE", "Fallback profiles must not cycle.", 422
                )
            visited.add(current)
            row = await session.execute(
                select(ModelProfile.id, ModelProfile.fallback_profile_id).where(
                    ModelProfile.id == current, ModelProfile.workspace_id == workspace_id
                )
            )
            found = row.one_or_none()
            if found is None:
                _not_found("MODEL_PROFILE_NOT_FOUND", "The fallback model profile was not found.")
            current = found.fallback_profile_id

    async def list_tool_catalog(self, context: WorkspaceExecutionContext) -> list[dict[str, Any]]:
        _require(context, "workspace_read")
        return [_catalog_entry(identity) for identity in BUILTIN_TOOL_CATALOG]

    async def list_tools(
        self, session: AsyncSession, context: WorkspaceExecutionContext
    ) -> list[dict[str, Any]]:
        workspace_id = _require(context, "workspace_read")
        tools = list(
            await session.scalars(
                select(Tool)
                .where(Tool.workspace_id == workspace_id)
                .order_by(Tool.created_at, Tool.id)
            )
        )
        revisions = list(
            await session.scalars(
                select(ToolRevision)
                .where(ToolRevision.workspace_id == workspace_id)
                .order_by(ToolRevision.tool_id, ToolRevision.revision_number.desc())
            )
        )
        latest: dict[UUID, ToolRevision] = {}
        for revision in revisions:
            latest.setdefault(revision.tool_id, revision)
        return [self._tool_projection(tool, latest.get(tool.id)) for tool in tools]

    async def get_tool(
        self, session: AsyncSession, context: WorkspaceExecutionContext, tool_id: UUID
    ) -> dict[str, Any]:
        workspace_id = _require(context, "workspace_read")
        tool = await self._tool(session, workspace_id, tool_id)
        revision = await session.scalar(
            select(ToolRevision)
            .where(ToolRevision.workspace_id == workspace_id, ToolRevision.tool_id == tool_id)
            .order_by(ToolRevision.revision_number.desc())
            .limit(1)
        )
        return self._tool_projection(tool, revision)

    async def create_tool(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        identity: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        workspace_id = _require(context, "tool_create")
        spec = _catalog_spec(identity)
        tool = Tool(
            workspace_id=workspace_id,
            name=name.strip() if name is not None else identity,
            description=description if description is not None else spec["description"],
            enabled=True,
        )
        session.add(tool)
        try:
            await session.flush()
            revision = ToolRevision(
                workspace_id=workspace_id,
                tool_id=tool.id,
                revision_number=1,
                spec=spec,
                spec_hash=canonical_json_hash(spec),
                created_by=_principal_id(context),
            )
            session.add(revision)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "TOOL_CREATE_FAILED", "The tool could not be created.", 409
            ) from exc
        return self._tool_projection(tool, revision)

    async def patch_tool(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        tool_id: UUID,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        workspace_id = _require(context, "tool_edit")
        tool = await self._tool(session, workspace_id, tool_id, for_update=True)
        for key, value in values.items():
            setattr(tool, key, value.strip() if key == "name" else value)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "TOOL_UPDATE_FAILED", "The tool could not be updated.", 409
            ) from exc
        return await self.get_tool(session, context, tool_id)

    async def list_tool_revisions(
        self, session: AsyncSession, context: WorkspaceExecutionContext, tool_id: UUID
    ) -> list[ToolRevision]:
        workspace_id = _require(context, "workspace_read")
        await self._tool(session, workspace_id, tool_id)
        revisions = list(
            await session.scalars(
                select(ToolRevision)
                .where(ToolRevision.workspace_id == workspace_id, ToolRevision.tool_id == tool_id)
                .order_by(ToolRevision.revision_number)
            )
        )
        for revision in revisions:
            _safe_spec(revision)
        return revisions

    async def get_tool_revision(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        tool_id: UUID,
        revision_id: UUID,
    ) -> ToolRevision:
        workspace_id = _require(context, "workspace_read")
        await self._tool(session, workspace_id, tool_id)
        revision = await session.scalar(
            select(ToolRevision).where(
                ToolRevision.id == revision_id,
                ToolRevision.workspace_id == workspace_id,
                ToolRevision.tool_id == tool_id,
            )
        )
        if revision is None:
            _not_found("TOOL_REVISION_NOT_FOUND", "The tool revision was not found.")
        _safe_spec(revision)
        return revision

    async def get_knowledge_bindings(
        self, session: AsyncSession, context: WorkspaceExecutionContext, agent_id: UUID
    ) -> list[AgentKnowledgeBinding]:
        workspace_id = _require(context, "workspace_read")
        await self._agent(session, workspace_id, agent_id)
        return list(
            await session.scalars(
                select(AgentKnowledgeBinding)
                .where(
                    AgentKnowledgeBinding.workspace_id == workspace_id,
                    AgentKnowledgeBinding.agent_id == agent_id,
                )
                .order_by(AgentKnowledgeBinding.knowledge_base_id)
            )
        )

    async def replace_knowledge_bindings(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent_id: UUID,
        bindings: list[dict[str, Any]],
    ) -> list[AgentKnowledgeBinding]:
        workspace_id = _require(context, "agent_edit")
        await self._validate_knowledge_bindings(session, workspace_id, bindings)
        agent = await self._agent(session, workspace_id, agent_id, for_update=True)
        del agent
        await session.execute(
            delete(AgentKnowledgeBinding).where(
                AgentKnowledgeBinding.workspace_id == workspace_id,
                AgentKnowledgeBinding.agent_id == agent_id,
            )
        )
        session.add_all(
            [
                AgentKnowledgeBinding(
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    knowledge_base_id=item["knowledge_base_id"],
                    binding_mode=item["binding_mode"],
                    snapshot_id=item["snapshot_id"],
                )
                for item in bindings
            ]
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "AGENT_KNOWLEDGE_BINDING_UPDATE_FAILED",
                "The knowledge bindings could not be updated.",
                409,
            ) from exc
        return await self.get_knowledge_bindings(session, context, agent_id)

    async def _validate_knowledge_bindings(
        self, session: AsyncSession, workspace_id: UUID, bindings: list[dict[str, Any]]
    ) -> None:
        knowledge_base_ids = [item["knowledge_base_id"] for item in bindings]
        if len(knowledge_base_ids) != len(set(knowledge_base_ids)):
            raise AgentHubError(
                "DUPLICATE_KNOWLEDGE_BINDING", "Knowledge bindings must be unique.", 422
            )
        for item in bindings:
            mode = item["binding_mode"]
            snapshot_id = item["snapshot_id"]
            if (mode == "LATEST" and snapshot_id is not None) or (
                mode == "PINNED" and snapshot_id is None
            ):
                raise AgentHubError(
                    "INVALID_KNOWLEDGE_BINDING", "The knowledge binding is invalid.", 422
                )
            knowledge_base = await session.scalar(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.id == item["knowledge_base_id"],
                    KnowledgeBase.workspace_id == workspace_id,
                )
            )
            if knowledge_base is None:
                _not_found("KNOWLEDGE_BASE_NOT_FOUND", "The knowledge base was not found.")
            if snapshot_id is not None:
                snapshot = await session.scalar(
                    select(KnowledgeSnapshot.id).where(
                        KnowledgeSnapshot.id == snapshot_id,
                        KnowledgeSnapshot.workspace_id == workspace_id,
                        KnowledgeSnapshot.knowledge_base_id == item["knowledge_base_id"],
                    )
                )
                if snapshot is None:
                    _not_found("SNAPSHOT_NOT_FOUND", "The knowledge snapshot was not found.")

    async def get_tool_bindings(
        self, session: AsyncSession, context: WorkspaceExecutionContext, agent_id: UUID
    ) -> list[AgentTool]:
        workspace_id = _require(context, "workspace_read")
        await self._agent(session, workspace_id, agent_id)
        return list(
            await session.scalars(
                select(AgentTool)
                .where(AgentTool.workspace_id == workspace_id, AgentTool.agent_id == agent_id)
                .order_by(AgentTool.tool_id)
            )
        )

    async def replace_tool_bindings(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent_id: UUID,
        bindings: list[dict[str, Any]],
    ) -> list[AgentTool]:
        workspace_id = _require(context, "agent_edit")
        await self._validate_tool_bindings(session, workspace_id, bindings)
        agent = await self._agent(session, workspace_id, agent_id, for_update=True)
        del agent
        await session.execute(
            delete(AgentTool).where(
                AgentTool.workspace_id == workspace_id, AgentTool.agent_id == agent_id
            )
        )
        session.add_all(
            [
                AgentTool(
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    tool_id=item["tool_id"],
                    tool_revision_id=item["tool_revision_id"],
                )
                for item in bindings
            ]
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "AGENT_TOOL_BINDING_UPDATE_FAILED",
                "The tool bindings could not be updated.",
                409,
            ) from exc
        return await self.get_tool_bindings(session, context, agent_id)

    async def _validate_tool_bindings(
        self, session: AsyncSession, workspace_id: UUID, bindings: list[dict[str, Any]]
    ) -> None:
        tool_ids = [item["tool_id"] for item in bindings]
        if len(tool_ids) != len(set(tool_ids)):
            raise AgentHubError("DUPLICATE_TOOL_BINDING", "Tool bindings must be unique.", 422)
        for item in bindings:
            tool = await session.scalar(
                select(Tool).where(
                    Tool.id == item["tool_id"], Tool.workspace_id == workspace_id
                )
            )
            if tool is None:
                _not_found("TOOL_NOT_FOUND", "The tool was not found.")
            if not tool.enabled:
                raise AgentHubError("TOOL_DISABLED", "The tool is disabled.", 422)
            revision_id = item["tool_revision_id"]
            if revision_id is None:
                revision = await session.scalar(
                    select(ToolRevision.id)
                    .where(
                        ToolRevision.workspace_id == workspace_id,
                        ToolRevision.tool_id == item["tool_id"],
                    )
                    .order_by(ToolRevision.revision_number.desc())
                    .limit(1)
                )
            else:
                revision = await session.scalar(
                    select(ToolRevision.id).where(
                        ToolRevision.id == revision_id,
                        ToolRevision.workspace_id == workspace_id,
                        ToolRevision.tool_id == item["tool_id"],
                    )
                )
            if revision is None:
                _not_found("TOOL_REVISION_NOT_FOUND", "The tool revision was not found.")

    async def list_documents(
        self, session: AsyncSession, context: WorkspaceExecutionContext, knowledge_base_id: UUID
    ) -> list[dict[str, Any]]:
        workspace_id = _require(context, "workspace_read")
        await self._knowledge_base(session, workspace_id, knowledge_base_id)
        documents = list(
            await session.scalars(
                select(Document)
                .where(
                    Document.workspace_id == workspace_id,
                    Document.knowledge_base_id == knowledge_base_id,
                )
                .order_by(Document.created_at, Document.id)
            )
        )
        if not documents:
            return []
        revisions = list(
            await session.scalars(
                select(DocumentRevision)
                .where(
                    DocumentRevision.workspace_id == workspace_id,
                    DocumentRevision.knowledge_base_id == knowledge_base_id,
                    DocumentRevision.document_id.in_([item.id for item in documents]),
                )
                .order_by(DocumentRevision.document_id, DocumentRevision.revision_number.desc())
            )
        )
        latest: dict[UUID, DocumentRevision] = {}
        for revision in revisions:
            latest.setdefault(revision.document_id, revision)
        return [
            self._document_projection(document, latest.get(document.id))
            for document in documents
        ]

    async def list_snapshots(
        self, session: AsyncSession, context: WorkspaceExecutionContext, knowledge_base_id: UUID
    ) -> list[dict[str, Any]]:
        workspace_id = _require(context, "workspace_read")
        await self._knowledge_base(session, workspace_id, knowledge_base_id)
        snapshots = list(
            await session.scalars(
                select(KnowledgeSnapshot)
                .where(
                    KnowledgeSnapshot.workspace_id == workspace_id,
                    KnowledgeSnapshot.knowledge_base_id == knowledge_base_id,
                )
                .order_by(KnowledgeSnapshot.created_at, KnowledgeSnapshot.id)
            )
        )
        counts = dict(
            (
                snapshot_id,
                count,
            )
            for snapshot_id, count in (
                await session.execute(
                    select(
                        KnowledgeSnapshotItem.snapshot_id,
                        func.count(KnowledgeSnapshotItem.document_revision_id),
                    )
                    .where(
                        KnowledgeSnapshotItem.workspace_id == workspace_id,
                        KnowledgeSnapshotItem.knowledge_base_id == knowledge_base_id,
                        KnowledgeSnapshotItem.snapshot_id.in_([item.id for item in snapshots]),
                    )
                    .group_by(KnowledgeSnapshotItem.snapshot_id)
                )
            )
        ) if snapshots else {}
        return [
            self._snapshot_projection(snapshot, int(counts.get(snapshot.id, 0)))
            for snapshot in snapshots
        ]

    async def get_snapshot(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        knowledge_base_id: UUID,
        snapshot_id: UUID,
    ) -> dict[str, Any]:
        workspace_id = _require(context, "workspace_read")
        await self._knowledge_base(session, workspace_id, knowledge_base_id)
        snapshot = await session.scalar(
            select(KnowledgeSnapshot).where(
                KnowledgeSnapshot.id == snapshot_id,
                KnowledgeSnapshot.workspace_id == workspace_id,
                KnowledgeSnapshot.knowledge_base_id == knowledge_base_id,
            )
        )
        if snapshot is None:
            _not_found("SNAPSHOT_NOT_FOUND", "The knowledge snapshot was not found.")
        item_rows = (
            await session.execute(
                select(
                    KnowledgeSnapshotItem.document_id,
                    KnowledgeSnapshotItem.document_revision_id,
                    Document.name,
                    DocumentRevision.revision_number,
                    DocumentRevision.ingestion_status,
                )
                .join(
                    Document,
                    (Document.id == KnowledgeSnapshotItem.document_id)
                    & (Document.workspace_id == KnowledgeSnapshotItem.workspace_id)
                    & (
                        Document.knowledge_base_id
                        == KnowledgeSnapshotItem.knowledge_base_id
                    ),
                )
                .join(
                    DocumentRevision,
                    (DocumentRevision.id == KnowledgeSnapshotItem.document_revision_id)
                    & (DocumentRevision.workspace_id == KnowledgeSnapshotItem.workspace_id)
                    & (
                        DocumentRevision.knowledge_base_id
                        == KnowledgeSnapshotItem.knowledge_base_id
                    ),
                )
                .where(
                    KnowledgeSnapshotItem.workspace_id == workspace_id,
                    KnowledgeSnapshotItem.knowledge_base_id == knowledge_base_id,
                    KnowledgeSnapshotItem.snapshot_id == snapshot_id,
                )
                .order_by(
                    KnowledgeSnapshotItem.document_id,
                    KnowledgeSnapshotItem.document_revision_id,
                )
            )
        ).all()
        return {
            **self._snapshot_projection(snapshot, len(item_rows)),
            "items": [
                {
                    "document_id": row.document_id,
                    "document_revision_id": row.document_revision_id,
                    "document_name": row.name,
                    "revision_number": row.revision_number,
                    "ingestion_status": row.ingestion_status,
                }
                for row in item_rows
            ],
        }

    @staticmethod
    async def _credential_row(
        session: AsyncSession, workspace_id: UUID, credential_id: UUID
    ) -> dict[str, Any] | None:
        result = await session.execute(
            select(
                ProviderCredential.id,
                ProviderCredential.workspace_id,
                ProviderCredential.provider,
                ProviderCredential.name,
                ProviderCredential.base_url,
                ProviderCredential.enabled,
                ProviderCredential.created_at,
            ).where(
                ProviderCredential.id == credential_id,
                ProviderCredential.workspace_id == workspace_id,
            )
        )
        row = result.one_or_none()
        return dict(row._mapping) if row is not None else None

    @staticmethod
    async def _tool(
        session: AsyncSession, workspace_id: UUID, tool_id: UUID, *, for_update: bool = False
    ) -> Tool:
        statement = select(Tool).where(Tool.id == tool_id, Tool.workspace_id == workspace_id)
        if for_update:
            statement = statement.with_for_update()
        tool = await session.scalar(statement)
        if tool is None:
            _not_found("TOOL_NOT_FOUND", "The tool was not found.")
        return tool

    @staticmethod
    def _tool_projection(tool: Tool, revision: ToolRevision | None) -> dict[str, Any]:
        projection = {
            "id": tool.id,
            "workspace_id": tool.workspace_id,
            "name": tool.name,
            "description": tool.description,
            "enabled": tool.enabled,
            "created_at": tool.created_at,
            "identity": None,
            "effect": None,
            "risk_level": None,
            "approval_policy": None,
            "execution_kind": None,
            "current_revision_id": revision.id if revision is not None else None,
            "current_revision_number": revision.revision_number if revision is not None else None,
            "current_spec_hash": revision.spec_hash if revision is not None else None,
        }
        if revision is not None:
            spec = _safe_spec(revision)
            catalog = _catalog_entry(spec["identity"])
            projection.update(
                {
                    "identity": spec["identity"],
                    "effect": spec["effect"],
                    "risk_level": spec["risk_level"],
                    "approval_policy": spec["approval_policy"],
                    "execution_kind": catalog["execution_kind"],
                }
            )
        return projection

    @staticmethod
    async def _agent(
        session: AsyncSession, workspace_id: UUID, agent_id: UUID, *, for_update: bool = False
    ) -> Agent:
        statement = select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
        if for_update:
            statement = statement.with_for_update()
        agent = await session.scalar(statement)
        if agent is None:
            _not_found("AGENT_NOT_FOUND", "The agent was not found.")
        return agent

    @staticmethod
    async def _knowledge_base(
        session: AsyncSession, workspace_id: UUID, knowledge_base_id: UUID
    ) -> KnowledgeBase:
        knowledge_base = await session.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.workspace_id == workspace_id,
            )
        )
        if knowledge_base is None:
            _not_found("KNOWLEDGE_BASE_NOT_FOUND", "The knowledge base was not found.")
        return knowledge_base

    @staticmethod
    def _document_projection(
        document: Document, revision: DocumentRevision | None
    ) -> dict[str, Any]:
        return {
            "id": document.id,
            "workspace_id": document.workspace_id,
            "knowledge_base_id": document.knowledge_base_id,
            "name": document.name,
            "created_at": document.created_at,
            "current_revision_id": revision.id if revision is not None else None,
            "current_revision_number": revision.revision_number if revision is not None else None,
            "current_revision_status": revision.ingestion_status if revision is not None else None,
            "current_revision_lifecycle_status": (
                revision.lifecycle_status if revision is not None else None
            ),
            "current_revision_created_at": revision.created_at if revision is not None else None,
        }

    @staticmethod
    def _snapshot_projection(snapshot: KnowledgeSnapshot, item_count: int) -> dict[str, Any]:
        return {
            "id": snapshot.id,
            "workspace_id": snapshot.workspace_id,
            "knowledge_base_id": snapshot.knowledge_base_id,
            "content_hash": snapshot.content_hash,
            "snapshot_schema_version": snapshot.snapshot_schema_version,
            "item_count": item_count,
            "created_at": snapshot.created_at,
        }


__all__ = ["BUILTIN_TOOL_CATALOG", "BuiltinToolCatalog", "ProductControlPlaneService"]
