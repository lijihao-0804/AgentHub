"""M4-A draft editing and immutable AgentVersion publication."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.models import (
    Agent,
    AgentKnowledgeBinding,
    AgentTool,
    AgentVersion,
    Tool,
    ToolRevision,
)
from packages.agent_runtime.runtime_config import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_RUNTIME_LIMITS,
    MAX_RUNTIME_LIMITS,
)
from packages.agent_runtime.tool_revisions import validate_tool_spec
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.snapshots import KnowledgeSnapshotService, ResolvedKnowledgeSnapshot
from packages.model_gateway.capabilities import CapabilityRequirements
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.profile_resolution import (
    ModelProfileResolver,
    ResolvedModelProfile,
)
from packages.model_gateway.repositories import SqlAlchemyModelGatewayRepository
from packages.tools.validation import validate_executable_tool_spec

SPEC_SCHEMA_VERSION = 1
DEFAULT_RETRIEVAL_CONFIG: dict[str, Any] = {
    "embedding_model": "BAAI/bge-m3",
    "reranker_model": "BAAI/bge-reranker-v2-m3",
    "dense_top_k": 30,
    "sparse_top_k": 30,
    "candidate_top_k": 20,
    "final_top_k": 6,
}
DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    **DEFAULT_RUNTIME_LIMITS,
    "context_budget": DEFAULT_CONTEXT_BUDGET.copy(),
}
_RUNTIME_KEYS = frozenset(DEFAULT_RUNTIME_CONFIG)
_CONTEXT_BUDGET_KEYS = frozenset(DEFAULT_RUNTIME_CONFIG["context_budget"])
_RETRIEVAL_KEYS = frozenset(DEFAULT_RETRIEVAL_CONFIG)


@dataclass(frozen=True, slots=True)
class PublishedAgentVersion:
    id: UUID
    agent_id: UUID
    workspace_id: UUID
    version_number: int
    resolved_spec_hash: str
    created_at: Any


class _LatestSnapshotMaterializationRequired(Exception):
    """The authoritative draft gained a LATEST binding during the publish boundary."""


class AgentPublishService:
    async def create_draft(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        *,
        name: str,
        system_prompt: str,
        model_profile_id: UUID,
        description: str | None = None,
        prompt_version: int = 1,
        knowledge_binding_mode: str = "PINNED",
        model_retry_policy: Mapping[str, Any] | None = None,
        retrieval_config: Mapping[str, Any] | None = None,
        runtime_config: Mapping[str, Any] | None = None,
    ) -> Agent:
        self._require_permission(context, "agent_create")
        workspace_id = _workspace_id(context)
        self._validate_draft_fields(
            name=name,
            system_prompt=system_prompt,
            prompt_version=prompt_version,
            knowledge_binding_mode=knowledge_binding_mode,
        )
        repository = SqlAlchemyModelGatewayRepository(session)
        if await repository.get_model_profile(context, model_profile_id) is None:
            raise _model_profile_unavailable()
        agent = Agent(
            workspace_id=workspace_id,
            name=name.strip(),
            description=description,
            system_prompt=system_prompt,
            prompt_version=prompt_version,
            model_profile_id=model_profile_id,
            knowledge_binding_mode=knowledge_binding_mode,
            model_retry_policy=_validate_retry_policy(model_retry_policy or {"max_attempts": 1}),
            retrieval_config=_validate_retrieval_config(retrieval_config or {}),
            runtime_config=_validate_runtime_config(runtime_config or {}),
        )
        session.add(agent)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "AGENT_CREATE_FAILED", "The agent could not be created.", 409
            ) from exc
        return agent

    async def list_drafts(
        self, session: AsyncSession, context: WorkspaceExecutionContext
    ) -> list[Agent]:
        workspace_id = _workspace_id(context)
        result = await session.scalars(
            select(Agent)
            .where(Agent.workspace_id == workspace_id)
            .order_by(Agent.created_at, Agent.id)
        )
        return list(result)

    async def get_draft(
        self, session: AsyncSession, context: WorkspaceExecutionContext, agent_id: UUID
    ) -> Agent:
        agent = await self._load_agent(session, context, agent_id)
        if agent is None:
            raise AgentHubError("AGENT_NOT_FOUND", "The agent was not found.", 404)
        return agent

    async def update_draft(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent_id: UUID,
        values: Mapping[str, Any],
    ) -> Agent:
        self._require_permission(context, "agent_edit")
        agent = await self._load_agent(session, context, agent_id, for_update=True)
        if agent is None:
            raise AgentHubError("AGENT_NOT_FOUND", "The agent was not found.", 404)
        allowed = {
            "name",
            "description",
            "system_prompt",
            "prompt_version",
            "model_profile_id",
            "knowledge_binding_mode",
            "model_retry_policy",
            "retrieval_config",
            "runtime_config",
        }
        if not set(values).issubset(allowed):
            raise AgentHubError("INVALID_AGENT_UPDATE", "The agent update is invalid.", 422)
        proposed = {key: getattr(agent, key) for key in allowed}
        proposed.update(values)
        self._validate_draft_fields(
            name=proposed["name"],
            system_prompt=proposed["system_prompt"],
            prompt_version=proposed["prompt_version"],
            knowledge_binding_mode=proposed["knowledge_binding_mode"],
        )
        if "model_profile_id" in values:
            repository = SqlAlchemyModelGatewayRepository(session)
            if await repository.get_model_profile(context, values["model_profile_id"]) is None:
                raise _model_profile_unavailable()
        proposed["model_retry_policy"] = _validate_retry_policy(proposed["model_retry_policy"])
        proposed["retrieval_config"] = _validate_retrieval_config(proposed["retrieval_config"])
        proposed["runtime_config"] = _validate_runtime_config(proposed["runtime_config"])
        for key, value in values.items():
            setattr(agent, key, value)
        agent.model_retry_policy = proposed["model_retry_policy"]
        agent.retrieval_config = proposed["retrieval_config"]
        agent.runtime_config = proposed["runtime_config"]
        await session.commit()
        return agent

    async def publish(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent_id: UUID,
    ) -> PublishedAgentVersion:
        self._require_permission(context, "agent_edit")
        workspace_id = _workspace_id(context)
        for _ in range(3):
            # LATEST materialization is intentionally outside the publish lock. The existing
            # Snapshot service owns its commit boundary and must not be made M4-aware.
            materialized_snapshots = await self._materialize_latest_snapshots(
                session, context, agent_id
            )
            # Materialization may have committed and therefore ended the transaction that
            # existed before this call. Start the authoritative publish transaction afresh.
            await session.rollback()
            agent = await self._load_agent(session, context, agent_id, for_update=True)
            if agent is None:
                raise AgentHubError("AGENT_NOT_FOUND", "The agent was not found.", 404)
            self._validate_draft_fields(
                name=agent.name,
                system_prompt=agent.system_prompt,
                prompt_version=agent.prompt_version,
                knowledge_binding_mode=agent.knowledge_binding_mode,
            )
            resolved_model = await self._resolve_model(session, context, agent)
            try:
                snapshots = await self._resolve_knowledge(
                    session,
                    context,
                    agent,
                    materialized_snapshots=materialized_snapshots,
                )
            except _LatestSnapshotMaterializationRequired:
                # The authoritative draft changed between phases. Release this transaction and
                # materialize the newly observed LATEST selectors before locking again.
                await session.rollback()
                continue
            tools = await self._resolve_tools(session, context, agent)
            resolved_spec = _resolved_spec(
                agent=agent,
                model=resolved_model,
                snapshots=snapshots,
                tools=tools,
            )
            resolved_spec_hash = canonical_json_hash(resolved_spec)
            version_number = (
                await session.scalar(
                    select(func.max(AgentVersion.version_number)).where(
                        AgentVersion.agent_id == agent.id,
                        AgentVersion.workspace_id == workspace_id,
                    )
                )
                or 0
            ) + 1
            version = AgentVersion(
                workspace_id=workspace_id,
                agent_id=agent.id,
                version_number=version_number,
                spec_schema_version=SPEC_SCHEMA_VERSION,
                resolved_spec=resolved_spec,
                resolved_spec_hash=resolved_spec_hash,
                created_by=_principal_id(context),
            )
            session.add(version)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise AgentHubError(
                    "AGENT_VERSION_CONFLICT", "The agent version could not be published.", 409
                ) from exc
            return PublishedAgentVersion(
                id=version.id,
                agent_id=version.agent_id,
                workspace_id=version.workspace_id,
                version_number=version.version_number,
                resolved_spec_hash=version.resolved_spec_hash,
                created_at=version.created_at,
            )
        raise AgentHubError(
            "AGENT_PUBLISH_CONFLICT",
            "The agent draft changed while its knowledge selectors were being materialized.",
            409,
        )

    async def list_versions(
        self, session: AsyncSession, context: WorkspaceExecutionContext, agent_id: UUID
    ) -> list[AgentVersion]:
        await self.get_draft(session, context, agent_id)
        workspace_id = _workspace_id(context)
        result = await session.scalars(
            select(AgentVersion)
            .where(
                AgentVersion.workspace_id == workspace_id,
                AgentVersion.agent_id == agent_id,
            )
            .order_by(AgentVersion.version_number)
        )
        return list(result)

    async def _resolve_model(
        self, session: AsyncSession, context: WorkspaceExecutionContext, agent: Agent
    ) -> tuple[tuple[ResolvedModelProfile, ...], dict[UUID, str]]:
        repository = SqlAlchemyModelGatewayRepository(session)
        resolver = ModelProfileResolver(repository)
        bindings = await session.scalars(
            select(AgentTool).where(
                AgentTool.workspace_id == agent.workspace_id,
                AgentTool.agent_id == agent.id,
            )
        )
        required = CapabilityRequirements(
            required=frozenset({"tool_calling"} if list(bindings) else set())
        )
        try:
            chain = await resolver.resolve_chain(
                context,
                agent.model_profile_id,
                required_capabilities=required,
            )
        except ModelGatewayError as exc:
            if exc.code == ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH:
                raise AgentHubError(
                    "MODEL_CAPABILITY_MISMATCH",
                    "The selected model chain lacks a required capability.",
                    422,
                ) from None
            raise _model_profile_unavailable() from None
        providers: dict[UUID, str] = {}
        for profile in chain:
            credential = await repository.get_provider_credential(
                context, profile.provider_credential_id
            )
            if credential is None or not credential.enabled:
                raise _model_profile_unavailable()
            providers[profile.id] = credential.provider
        return chain, providers

    async def _resolve_knowledge(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent: Agent,
        *,
        materialized_snapshots: Mapping[UUID, ResolvedKnowledgeSnapshot],
    ) -> tuple[ResolvedKnowledgeSnapshot, ...]:
        bindings = await self._load_knowledge_bindings(session, agent)
        service = KnowledgeSnapshotService()
        resolved: list[ResolvedKnowledgeSnapshot] = []
        for binding in bindings:
            selector: UUID | str
            if binding.binding_mode == "LATEST":
                snapshot = materialized_snapshots.get(binding.knowledge_base_id)
                if snapshot is None:
                    raise _LatestSnapshotMaterializationRequired
                resolved.append(snapshot)
                continue
            elif binding.snapshot_id is not None:
                selector = binding.snapshot_id
            else:
                raise AgentHubError(
                    "INVALID_KNOWLEDGE_BINDING", "Pinned knowledge requires a snapshot.", 422
                )
            resolved.append(
                await service.resolve_snapshot(
                    session,
                    context,
                    binding.knowledge_base_id,
                    selector,
                )
            )
        return tuple(resolved)

    async def _materialize_latest_snapshots(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent_id: UUID,
    ) -> dict[UUID, ResolvedKnowledgeSnapshot]:
        agent = await self._load_agent(session, context, agent_id)
        if agent is None:
            raise AgentHubError("AGENT_NOT_FOUND", "The agent was not found.", 404)
        bindings = await self._load_knowledge_bindings(session, agent)
        service = KnowledgeSnapshotService()
        resolved: dict[UUID, ResolvedKnowledgeSnapshot] = {}
        binding_selectors = [
            (binding.binding_mode, binding.knowledge_base_id) for binding in bindings
        ]
        for binding_mode, knowledge_base_id in binding_selectors:
            if binding_mode == "LATEST":
                resolved[knowledge_base_id] = await service.resolve_snapshot(
                    session,
                    context,
                    knowledge_base_id,
                    "LATEST",
                )
        return resolved

    @staticmethod
    async def _load_knowledge_bindings(
        session: AsyncSession, agent: Agent
    ) -> list[AgentKnowledgeBinding]:
        result = await session.scalars(
            select(AgentKnowledgeBinding)
            .where(
                AgentKnowledgeBinding.workspace_id == agent.workspace_id,
                AgentKnowledgeBinding.agent_id == agent.id,
            )
            .order_by(AgentKnowledgeBinding.knowledge_base_id)
        )
        return list(result)

    async def _resolve_tools(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent: Agent,
    ) -> tuple[dict[str, Any], ...]:
        bindings = await session.scalars(
            select(AgentTool)
            .where(AgentTool.workspace_id == agent.workspace_id, AgentTool.agent_id == agent.id)
            .order_by(AgentTool.tool_id)
        )
        projections: list[dict[str, Any]] = []
        for binding in bindings:
            tool = await session.scalar(
                select(Tool).where(
                    Tool.id == binding.tool_id,
                    Tool.workspace_id == agent.workspace_id,
                )
            )
            if tool is None or not tool.enabled:
                raise AgentHubError("TOOL_NOT_FOUND", "The tool was not found.", 404)
            statement = select(ToolRevision).where(
                ToolRevision.workspace_id == agent.workspace_id,
                ToolRevision.tool_id == binding.tool_id,
            )
            if binding.tool_revision_id is not None:
                statement = statement.where(ToolRevision.id == binding.tool_revision_id)
            else:
                statement = statement.order_by(desc(ToolRevision.revision_number)).limit(1)
            revision = await session.scalar(statement)
            if revision is None:
                raise AgentHubError(
                    "TOOL_REVISION_NOT_FOUND", "The tool revision was not found.", 404
                )
            safe_spec = validate_tool_spec(revision.spec)
            if canonical_json_hash(safe_spec) != revision.spec_hash:
                raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
            executable_spec = validate_executable_tool_spec(safe_spec)
            projections.append(
                {
                    "tool_revision_id": str(revision.id),
                    "tool_spec_hash": revision.spec_hash,
                    "effect": executable_spec["effect"],
                    "risk_level": executable_spec["risk_level"],
                    "approval_policy": executable_spec["approval_policy"],
                }
            )
        return tuple(projections)

    @staticmethod
    async def _load_agent(
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent_id: UUID,
        *,
        for_update: bool = False,
    ) -> Agent | None:
        statement = select(Agent).where(
            Agent.id == agent_id,
            Agent.workspace_id == _workspace_id(context),
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    @staticmethod
    def _require_permission(context: WorkspaceExecutionContext, permission: str) -> None:
        if permission not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _validate_draft_fields(
        *, name: str, system_prompt: str, prompt_version: int, knowledge_binding_mode: str
    ) -> None:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
            raise AgentHubError("INVALID_AGENT", "The agent draft is invalid.", 422)
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise AgentHubError("INVALID_AGENT", "The agent draft is invalid.", 422)
        if (
            isinstance(prompt_version, bool)
            or not isinstance(prompt_version, int)
            or prompt_version < 1
        ):
            raise AgentHubError("INVALID_AGENT", "The agent draft is invalid.", 422)
        if knowledge_binding_mode not in {"PINNED", "LATEST"}:
            raise AgentHubError(
                "INVALID_KNOWLEDGE_BINDING",
                "The knowledge binding mode is invalid.",
                422,
            )


def _resolved_spec(
    *,
    agent: Agent,
    model: tuple[tuple[ResolvedModelProfile, ...], dict[UUID, str]],
    snapshots: tuple[ResolvedKnowledgeSnapshot, ...],
    tools: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    chain, providers = model
    primary = chain[0]
    retry_policy = _validate_retry_policy(agent.model_retry_policy)
    primary_projection = _profile_projection(primary, providers[primary.id])
    primary_projection.update(
        {
            "profile_id": str(primary.id),
            "credential_ref": str(primary.provider_credential_id),
            "retry_policy": retry_policy,
            "fallback_chain": [str(profile.id) for profile in chain[1:]],
            "fallback_profiles": [
                {
                    **_profile_projection(profile, providers[profile.id]),
                    "profile_id": str(profile.id),
                    "credential_ref": str(profile.provider_credential_id),
                }
                for profile in chain[1:]
            ],
        }
    )
    snapshot_refs = [
        {"snapshot_id": str(snapshot.snapshot_id), "snapshot_hash": snapshot.content_hash}
        for snapshot in snapshots
    ]
    retrieval = _validate_retrieval_config(agent.retrieval_config)
    retrieval.update(
        {
            "knowledge_binding_mode": "PINNED",
            "knowledge_snapshot_ids": [item["snapshot_id"] for item in snapshot_refs],
            "knowledge_snapshots": snapshot_refs,
        }
    )
    return {
        "spec_schema_version": SPEC_SCHEMA_VERSION,
        "model": primary_projection,
        "prompt": {
            "system_prompt": agent.system_prompt,
            "prompt_version": agent.prompt_version,
        },
        "retrieval": retrieval,
        "tools": list(tools),
        "runtime": _validate_runtime_config(agent.runtime_config),
    }


def _profile_projection(profile: ResolvedModelProfile, provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": profile.model,
        "temperature": float(profile.temperature),
        "max_tokens": profile.max_tokens,
        "timeout_seconds": float(profile.timeout_seconds),
        "capabilities": {
            "tool_calling": profile.capabilities.tool_calling,
            "streaming": profile.capabilities.streaming,
            "structured_output": profile.capabilities.structured_output,
            "vision": profile.capabilities.vision,
            "max_context_tokens": profile.capabilities.max_context_tokens,
        },
    }


def _validate_retry_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retry policy is invalid.", 422)
    allowed = {"max_attempts"}
    if not set(value).issubset(allowed):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retry policy is invalid.", 422)
    attempts = value.get("max_attempts", 1)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 5:
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retry policy is invalid.", 422)
    return {"max_attempts": attempts}


def _validate_retrieval_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not set(value).issubset(_RETRIEVAL_KEYS):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    result = {**DEFAULT_RETRIEVAL_CONFIG, **dict(value)}
    for key in ("dense_top_k", "sparse_top_k", "candidate_top_k", "final_top_k"):
        item = result[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    if result["final_top_k"] > result["candidate_top_k"]:
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    return result


def _validate_runtime_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not set(value).issubset(_RUNTIME_KEYS):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    result = {**DEFAULT_RUNTIME_CONFIG, **dict(value)}
    context_budget = result.get("context_budget")
    if not isinstance(context_budget, Mapping) or not set(context_budget).issubset(
        _CONTEXT_BUDGET_KEYS
    ):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    result["context_budget"] = {
        **DEFAULT_RUNTIME_CONFIG["context_budget"],
        **dict(context_budget),
    }
    for key in _RUNTIME_KEYS - {"context_budget"}:
        item = result[key]
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 1 <= item <= MAX_RUNTIME_LIMITS[key]
        ):
            raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    for item in result["context_budget"].values():
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    return result


def _model_profile_unavailable() -> AgentHubError:
    return AgentHubError(
        "MODEL_PROFILE_DISABLED",
        "The model profile is unavailable.",
        422,
    )


def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
    try:
        return UUID(context.workspace_id)
    except ValueError:
        raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None


def _principal_id(context: WorkspaceExecutionContext) -> UUID:
    try:
        if context.user_id is None:
            raise ValueError
        return UUID(context.user_id)
    except ValueError:
        raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401) from None


__all__ = [
    "AgentPublishService",
    "DEFAULT_RETRIEVAL_CONFIG",
    "DEFAULT_RUNTIME_CONFIG",
    "PublishedAgentVersion",
    "SPEC_SCHEMA_VERSION",
]
