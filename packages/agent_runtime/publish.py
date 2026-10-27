"""M4-A draft editing and immutable AgentVersion publication."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.context_policy import (
    normalize_context_policy_request,
    resolve_context_policy,
)
from packages.agent_runtime.models import (
    Agent,
    AgentKnowledgeBinding,
    AgentTool,
    AgentVersion,
    AgentWikiBinding,
    Tool,
    ToolRevision,
)
from packages.agent_runtime.runtime_config import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_MEMORY_CONFIG,
    DEFAULT_MEMORY_TOKENS,
    DEFAULT_RUN_COST_LIMIT_MICRO_USD,
    DEFAULT_RUNTIME_LIMITS,
    MAX_CONTEXT_BUDGET,
    MAX_RUN_COST_LIMIT_MICRO_USD,
    MAX_RUNTIME_LIMITS,
    derive_context_budget,
    derive_memory_tokens,
)
from packages.agent_runtime.tool_revisions import validate_tool_spec
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings, get_settings
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.contracts import RetrievalStrategy
from packages.knowledge.snapshots import KnowledgeSnapshotService, ResolvedKnowledgeSnapshot
from packages.mcp.models import McpConnection
from packages.model_gateway.adapters.local_context_catalog import (
    apply_local_context_catalog,
    load_local_context_catalog,
)
from packages.model_gateway.capabilities import CapabilityRequirements
from packages.model_gateway.context_metadata import (
    InputOutputSemantics,
    context_metadata_to_mapping,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.profile_resolution import (
    ModelProfileResolver,
    ResolvedModelProfile,
)
from packages.model_gateway.repositories import SqlAlchemyModelGatewayRepository
from packages.tools.validation import validate_executable_tool_spec
from packages.wiki.models import WikiCompilation
from packages.wiki.service import WikiService

SPEC_SCHEMA_VERSION = 2
ADAPTIVE_SPEC_SCHEMA_VERSION = 3
HISTORY_SPEC_SCHEMA_VERSION = 4
_WIKI_LATEST_SCAN_BATCH = 10
DEFAULT_RETRIEVAL_CONFIG: dict[str, Any] = {
    "embedding_model": "BAAI/bge-m3",
    "reranker_model": "BAAI/bge-reranker-v2-m3",
    "retrieval_strategy": RetrievalStrategy.HYBRID_RERANK.value,
    "dense_top_k": 30,
    "sparse_top_k": 30,
    "candidate_top_k": 20,
    "final_top_k": 6,
}
DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    **DEFAULT_RUNTIME_LIMITS,
    "max_cost_micro_usd": DEFAULT_RUN_COST_LIMIT_MICRO_USD,
    "context_budget": DEFAULT_CONTEXT_BUDGET.copy(),
    "memory": DEFAULT_MEMORY_CONFIG.copy(),
}
_RUNTIME_KEYS = frozenset(DEFAULT_RUNTIME_CONFIG) | {
    "context_policy",
    "context_budget_sources",
    "history_policy",
}
# Validated on their own terms: neither is a step count. ``max_cost_micro_usd``
# is the one runtime key that may be ``None``; ``memory`` is a block of booleans
# with a ceiling that means nothing.
_RUNTIME_SCALAR_KEYS = _RUNTIME_KEYS - {
    "context_budget",
    "context_budget_sources",
    "context_policy",
    "history_policy",
    "max_cost_micro_usd",
    "memory",
}
_CONTEXT_BUDGET_KEYS = frozenset(DEFAULT_RUNTIME_CONFIG["context_budget"])
_MEMORY_KEYS = frozenset((*DEFAULT_MEMORY_CONFIG, "max_memory_tokens", "max_memory_tokens_source"))
_CONFIGURATION_SOURCES = frozenset({"UNSET", "DEFAULT", "CUSTOM"})
_RETRIEVAL_OPTIONAL_KEYS = frozenset(
    {"rerank_top_k", "max_chunks_per_document", "context_budget_bytes"}
)
_RETRIEVAL_KEYS = (
    frozenset(DEFAULT_RETRIEVAL_CONFIG)
    | _RETRIEVAL_OPTIONAL_KEYS
    | {"evidence_projection", "evidence_set_selector"}
)


@dataclass(frozen=True, slots=True)
class PublishedAgentVersion:
    id: UUID
    agent_id: UUID
    workspace_id: UUID
    version_number: int
    resolved_spec_hash: str
    created_at: Any


class AgentPublishService:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

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
            retrieval_config=_validate_retrieval_config(
                retrieval_config or {},
                settings=self.settings,
            ),
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
        proposed["retrieval_config"] = _validate_retrieval_config(
            proposed["retrieval_config"],
            settings=self.settings,
        )
        proposed["runtime_config"] = _validate_runtime_config(
            proposed["runtime_config"], legacy_stored="runtime_config" not in values
        )
        for key, value in values.items():
            setattr(agent, key, value)
        agent.model_retry_policy = proposed["model_retry_policy"]
        agent.retrieval_config = proposed["retrieval_config"]
        agent.runtime_config = proposed["runtime_config"]
        await session.commit()
        # `updated_at` is filled in by the database on UPDATE, so the flush
        # expires it no matter what `expire_on_commit` says.  Reload it here,
        # inside the async context, rather than letting the response serializer
        # touch an expired attribute where no greenlet is available.
        await session.refresh(agent)
        return agent

    async def publish(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent_id: UUID,
    ) -> PublishedAgentVersion:
        self._require_permission(context, "agent_edit")
        workspace_id = _workspace_id(context)
        agent = await self._load_agent(session, context, agent_id, for_update=True)
        if agent is None:
            raise AgentHubError("AGENT_NOT_FOUND", "The agent was not found.", 404)
        resolved_spec, resolved_spec_hash = await self._resolve_draft_spec(session, context, agent)
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
            spec_schema_version=resolved_spec["spec_schema_version"],
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

    async def preflight(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent_id: UUID,
    ) -> dict[str, Any]:
        self._require_permission(context, "agent_edit")
        agent = await self._load_agent(session, context, agent_id)
        if agent is None:
            raise AgentHubError("AGENT_NOT_FOUND", "The agent was not found.", 404)
        resolved_spec, resolved_spec_hash = await self._resolve_draft_spec(session, context, agent)
        return {
            "status": "READY",
            "agent_id": agent.id,
            "workspace_id": agent.workspace_id,
            "draft_updated_at": agent.updated_at,
            "spec_schema_version": resolved_spec["spec_schema_version"],
            "resolved_spec_hash": resolved_spec_hash,
            "resolved_spec": resolved_spec,
        }

    async def derive_draft_values(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent_id: UUID,
        version_id: UUID,
    ) -> dict[str, Any]:
        """Extract an editable-draft payload out of a published version.

        Derive does not silently overwrite the draft: it returns the values
        the caller reviews and then applies through the normal draft update
        path, where every field is validated again. Knowledge and tool
        bindings stay where they are managed (their own editors); only the
        draft-owned fields come back.
        """
        self._require_permission(context, "agent_edit")
        version = await self.get_version(session, context, agent_id, version_id)
        spec = version.resolved_spec or {}
        prompt = dict(spec.get("prompt") or {})
        model = dict(spec.get("model") or {})
        retrieval_source = dict(spec.get("retrieval") or {})
        retrieval = {
            key: value
            for key, value in retrieval_source.items()
            if key
            not in {
                "knowledge_binding_mode",
                "knowledge_snapshot_ids",
                "knowledge_snapshots",
                "knowledge_bindings",
            }
        }
        return {
            "source_version_id": str(version.id),
            "source_version_number": version.version_number,
            "source_resolved_spec_hash": version.resolved_spec_hash,
            "values": {
                "system_prompt": prompt.get("system_prompt"),
                "prompt_version": prompt.get("prompt_version"),
                "model_profile_id": model.get("profile_id"),
                "knowledge_binding_mode": retrieval_source.get("knowledge_binding_mode"),
                "model_retry_policy": model.get("retry_policy"),
                "retrieval_config": retrieval,
                "runtime_config": _draft_runtime_from_version(spec.get("runtime") or {}),
            },
        }

    async def _resolve_draft_spec(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent: Agent,
    ) -> tuple[dict[str, Any], str]:
        self._validate_draft_fields(
            name=agent.name,
            system_prompt=agent.system_prompt,
            prompt_version=agent.prompt_version,
            knowledge_binding_mode=agent.knowledge_binding_mode,
        )
        resolved_model = await self._resolve_model(session, context, agent)
        resolved_runtime = _resolved_runtime_config(agent.runtime_config, resolved_model)
        _validate_published_context_budget(resolved_runtime, resolved_model)
        bindings = await self._resolve_knowledge(session, context, agent)
        wiki_binding = await self._resolve_wiki_binding(session, context, agent)
        tools = await self._resolve_tools(
            session,
            context,
            agent,
            wiki_bound=wiki_binding is not None,
        )
        resolved_spec = _resolved_spec(
            agent=agent,
            model=resolved_model,
            bindings=bindings,
            tools=tools,
            wiki_binding=wiki_binding,
            resolved_runtime=resolved_runtime,
        )
        return resolved_spec, canonical_json_hash(resolved_spec)

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

    async def get_version(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent_id: UUID,
        version_id: UUID,
    ) -> AgentVersion:
        self._require_permission(context, "workspace_read")
        workspace_id = _workspace_id(context)
        version = await session.scalar(
            select(AgentVersion).where(
                AgentVersion.id == version_id,
                AgentVersion.workspace_id == workspace_id,
                AgentVersion.agent_id == agent_id,
            )
        )
        if version is None:
            raise AgentHubError("AGENT_VERSION_NOT_FOUND", "The agent version was not found.", 404)
        return version

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
        if agent.runtime_config.get("context_policy") is not None:
            try:
                catalog = (
                    load_local_context_catalog(self.settings.model_context_catalog_path)
                    if self.settings.model_context_catalog_path is not None
                    else None
                )
                constrained = []
                for profile in chain:
                    metadata = profile.capabilities.context_metadata
                    if metadata is None:
                        constrained.append(profile)
                        continue
                    # Source records and catalog references are generated by the
                    # server, not evidence supplied by an editable profile.
                    metadata = replace(metadata, catalog_ref=None, source_records=None)
                    entry = (
                        catalog.resolve(
                            provider=providers[profile.id],
                            model=profile.model,
                            deployment_id=metadata.deployment_id,
                        )
                        if catalog is not None
                        else None
                    )
                    metadata, total_context, _source = apply_local_context_catalog(
                        provider=providers[profile.id],
                        model=profile.model,
                        deployment_id=metadata.deployment_id,
                        catalog=catalog,
                        declared_metadata=metadata,
                        declared_max_context_tokens=profile.capabilities.max_context_tokens,
                    )
                    if total_context is None and (
                        entry is None
                        or entry.metadata.input_output_semantics
                        is not InputOutputSemantics.INDEPENDENT_INPUT_OUTPUT
                        or entry.metadata.max_input_tokens is None
                    ):
                        raise ValueError("input-only capacity needs an exact trusted catalog entry")
                    constrained.append(
                        replace(
                            profile,
                            capabilities=replace(
                                profile.capabilities,
                                context_metadata=metadata,
                                max_context_tokens=total_context,
                            ),
                        )
                    )
                chain = tuple(constrained)
            except (TypeError, ValueError):
                raise AgentHubError(
                    "INVALID_CONTEXT_POLICY",
                    "Model capability sources are invalid or insufficient for this policy.",
                    422,
                ) from None
        return chain, providers

    async def _resolve_knowledge(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent: Agent,
    ) -> tuple[dict[str, Any], ...]:
        bindings = await self._load_knowledge_bindings(session, agent)
        service = KnowledgeSnapshotService()
        resolved: list[dict[str, Any]] = []
        for binding in bindings:
            if binding.binding_mode == "LATEST":
                resolved.append(
                    {
                        "knowledge_base_id": str(binding.knowledge_base_id),
                        "binding_mode": "LATEST",
                    }
                )
                continue
            if binding.snapshot_id is None:
                raise AgentHubError(
                    "INVALID_KNOWLEDGE_BINDING", "Pinned knowledge requires a snapshot.", 422
                )
            snapshot = await service.resolve_snapshot(
                session,
                context,
                binding.knowledge_base_id,
                binding.snapshot_id,
            )
            resolved.append(
                {
                    "knowledge_base_id": str(binding.knowledge_base_id),
                    "binding_mode": "PINNED",
                    "snapshot_id": str(snapshot.snapshot_id),
                    "snapshot_hash": snapshot.content_hash,
                }
            )
        return tuple(resolved)

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

    async def _resolve_wiki_binding(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent: Agent,
    ) -> dict[str, Any] | None:
        """Resolve the draft Wiki binding once, at publish time.

        ``LATEST`` is resolved here and its result frozen into the spec: a
        published version is read forever after, so the binding it names must
        not move underneath historical runs or experiments. A publication
        whose sources include a revoked revision cannot be bound at all.
        """

        binding = await session.scalar(
            select(AgentWikiBinding).where(
                AgentWikiBinding.workspace_id == agent.workspace_id,
                AgentWikiBinding.agent_id == agent.id,
            )
        )
        if binding is None:
            return None
        wiki_context = context.model_copy(
            update={"permissions": context.permissions | frozenset({"knowledge_run"})}
        )
        if binding.binding_mode == "LATEST":
            compilation = await self._latest_publishable_wiki(
                session, wiki_context, agent.workspace_id, binding.knowledge_base_id
            )
            if compilation is None:
                raise AgentHubError(
                    "WIKI_BINDING_UNRESOLVED",
                    "No publishable Wiki publication exists for the bound knowledge base.",
                    422,
                )
        else:
            if binding.wiki_publication_id is None or binding.publication_hash is None:
                raise AgentHubError("INVALID_WIKI_BINDING", "The Wiki binding is invalid.", 422)
            compilation = await session.scalar(
                select(WikiCompilation).where(
                    WikiCompilation.workspace_id == agent.workspace_id,
                    WikiCompilation.knowledge_base_id == binding.knowledge_base_id,
                    WikiCompilation.id == binding.wiki_publication_id,
                )
            )
            if (
                compilation is None
                or compilation.status != "PUBLISHED"
                or compilation.content_hash != binding.publication_hash
            ):
                raise AgentHubError(
                    "WIKI_BINDING_INVALID",
                    "The bound Wiki publication does not match its frozen hash.",
                    422,
                )
        await WikiService().sources(session, wiki_context, compilation, require_current=False)
        return {
            "schema_version": 1,
            "binding_mode": binding.binding_mode,
            "knowledge_base_id": str(binding.knowledge_base_id),
            "wiki_publication_id": str(compilation.id),
            "publication_hash": str(compilation.content_hash),
        }

    @staticmethod
    async def _latest_publishable_wiki(
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        workspace_id: UUID,
        knowledge_base_id: UUID,
    ) -> WikiCompilation | None:
        """The newest published compilation whose sources are not revoked.

        Revoked publications are skipped rather than fatal: a LATEST binding
        names "the current Wiki", and a tombstoned one is no longer it.
        """

        service = WikiService()
        cursor: tuple[Any, UUID] | None = None
        while True:
            statement = select(WikiCompilation).where(
                WikiCompilation.workspace_id == workspace_id,
                WikiCompilation.knowledge_base_id == knowledge_base_id,
                WikiCompilation.status == "PUBLISHED",
            )
            if cursor is not None:
                published_at, publication_id = cursor
                statement = statement.where(
                    (WikiCompilation.published_at < published_at)
                    | (
                        (WikiCompilation.published_at == published_at)
                        & (WikiCompilation.id > publication_id)
                    )
                )
            candidates = list(
                await session.scalars(
                    statement.order_by(
                        WikiCompilation.published_at.desc(),
                        WikiCompilation.id,
                    ).limit(_WIKI_LATEST_SCAN_BATCH)
                )
            )
            if not candidates:
                break
            for compilation in candidates:
                try:
                    await service.sources(
                        session,
                        context,
                        compilation,
                        require_current=False,
                    )
                except AgentHubError as error:
                    if error.code == "WIKI_SOURCE_REVOKED":
                        continue
                    raise
                return compilation
            last = candidates[-1]
            cursor = (last.published_at, last.id)
        return None

    async def _resolve_tools(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        agent: Agent,
        *,
        wiki_bound: bool,
    ) -> tuple[dict[str, Any], ...]:
        bindings = await session.scalars(
            select(AgentTool)
            .where(AgentTool.workspace_id == agent.workspace_id, AgentTool.agent_id == agent.id)
            .order_by(AgentTool.tool_id)
        )
        projections: list[dict[str, Any]] = []
        seen_identities: set[str] = set()
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
            identity = executable_spec["identity"]
            if identity == "query_wiki" and not wiki_bound:
                raise AgentHubError(
                    "WIKI_BINDING_REQUIRED",
                    "The query_wiki tool requires a published Wiki binding.",
                    422,
                )
            if identity in seen_identities:
                # The model proposes a call by identity and the runtime
                # dispatches by identity, so two bound tools answering to the
                # same name have no defined winner. Refusing at publish is the
                # only place that ambiguity can still be corrected by a human;
                # after this it would be decided by row order.
                raise AgentHubError(
                    "DUPLICATE_TOOL_IDENTITY",
                    "Two bound tools share the same identity.",
                    422,
                )
            seen_identities.add(identity)
            projection = {
                "tool_revision_id": str(revision.id),
                "tool_spec_hash": revision.spec_hash,
                "effect": executable_spec["effect"],
                "risk_level": executable_spec["risk_level"],
                "approval_policy": executable_spec["approval_policy"],
            }
            if executable_spec["kind"] == "mcp":
                # Only remote tools carry this. Builtin projections are left
                # byte-identical to what every already-published version froze.
                projection["source_kind"] = "mcp"
                await self._validate_mcp_connection(
                    session, agent.workspace_id, executable_spec["mcp"]["connection_id"]
                )
            projections.append(projection)
        return tuple(projections)

    @staticmethod
    async def _validate_mcp_connection(
        session: AsyncSession, workspace_id: UUID, connection_id: str
    ) -> None:
        """Refuse to publish a remote tool that could not be called today.

        Publishing is a promise that the frozen version is runnable. A tool
        pointing at a connection that has been deleted, belongs to another
        workspace, or has been deliberately switched off is not, and finding
        that out at publish time is much cheaper than finding it out mid-run.
        """

        record = await session.execute(
            select(McpConnection.enabled).where(
                McpConnection.id == UUID(connection_id),
                McpConnection.workspace_id == workspace_id,
            )
        )
        row = record.first()
        if row is None:
            raise AgentHubError(
                "MCP_CONNECTION_NOT_FOUND", "The MCP connection was not found.", 404
            )
        if not row.enabled:
            raise AgentHubError("MCP_CONNECTION_DISABLED", "The MCP connection is disabled.", 409)

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
    bindings: tuple[dict[str, Any], ...] | None = None,
    snapshots: tuple[ResolvedKnowledgeSnapshot, ...] | None = None,
    tools: tuple[dict[str, Any], ...],
    wiki_binding: dict[str, Any] | None = None,
    resolved_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chain, providers = model
    primary = chain[0]
    runtime = (
        _resolved_runtime_config(agent.runtime_config, model)
        if resolved_runtime is None
        else dict(resolved_runtime)
    )
    adaptive = "context_policy" in runtime
    retry_policy = _validate_retry_policy(agent.model_retry_policy)
    primary_projection = _profile_projection(
        primary, providers[primary.id], include_context_metadata=adaptive
    )
    primary_projection.update(
        {
            "profile_id": str(primary.id),
            "credential_ref": str(primary.provider_credential_id),
            "retry_policy": retry_policy,
            "fallback_chain": [str(profile.id) for profile in chain[1:]],
            "fallback_profiles": [
                {
                    **_profile_projection(
                        profile, providers[profile.id], include_context_metadata=adaptive
                    ),
                    "profile_id": str(profile.id),
                    "credential_ref": str(profile.provider_credential_id),
                }
                for profile in chain[1:]
            ],
        }
    )
    if bindings is None:
        bindings = tuple(
            {
                "knowledge_base_id": str(snapshot.knowledge_base_id),
                "binding_mode": "PINNED",
                "snapshot_id": str(snapshot.snapshot_id),
                "snapshot_hash": snapshot.content_hash,
            }
            for snapshot in (snapshots or ())
        )
    snapshot_refs = [
        {
            "snapshot_id": item["snapshot_id"],
            "snapshot_hash": item["snapshot_hash"],
        }
        for item in bindings
        if item["binding_mode"] == "PINNED"
    ]
    retrieval = _validate_retrieval_config(agent.retrieval_config)
    retrieval.update(
        {
            "knowledge_binding_mode": agent.knowledge_binding_mode,
            "knowledge_snapshot_ids": [item["snapshot_id"] for item in snapshot_refs],
            "knowledge_snapshots": snapshot_refs,
            "knowledge_bindings": list(bindings),
        }
    )
    spec: dict[str, Any] = {
        "spec_schema_version": (
            HISTORY_SPEC_SCHEMA_VERSION
            if runtime.get("history_policy") is not None
            else ADAPTIVE_SPEC_SCHEMA_VERSION
            if adaptive
            else SPEC_SCHEMA_VERSION
        ),
        "model": primary_projection,
        "prompt": {
            "system_prompt": agent.system_prompt,
            "prompt_version": agent.prompt_version,
        },
        "retrieval": retrieval,
        "tools": list(tools),
        "runtime": runtime,
    }
    # Absent means "never bound": every version published before Wiki binding
    # existed, and every agent without one, keeps its exact previous bytes.
    if wiki_binding is not None:
        spec["wiki_binding"] = wiki_binding
    return spec


def _profile_projection(
    profile: ResolvedModelProfile, provider: str, *, include_context_metadata: bool = False
) -> dict[str, Any]:
    result = {
        "provider": provider,
        "model": profile.model,
        "temperature": _canonical_float(profile.temperature),
        "max_tokens": profile.max_tokens,
        "timeout_seconds": _canonical_float(profile.timeout_seconds),
        "capabilities": {
            "tool_calling": profile.capabilities.tool_calling,
            "streaming": profile.capabilities.streaming,
            "structured_output": profile.capabilities.structured_output,
            "vision": profile.capabilities.vision,
            "max_context_tokens": profile.capabilities.max_context_tokens,
        },
    }
    context_metadata = getattr(profile.capabilities, "context_metadata", None)
    if include_context_metadata and context_metadata is not None:
        result["capabilities"]["context_metadata"] = context_metadata_to_mapping(context_metadata)
    return result


def _canonical_float(value: Any) -> float:
    numeric = float(value)
    return 0.0 if numeric == 0 else numeric


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


def _validate_retrieval_config(
    value: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not set(value).issubset(_RETRIEVAL_KEYS):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    explicitly_none = {key for key, item in value.items() if item is None}
    result = {**DEFAULT_RETRIEVAL_CONFIG, **dict(value)}
    active_settings = settings or get_settings()
    for key, configured in (
        ("embedding_model", active_settings.knowledge_embedding_model),
        ("reranker_model", active_settings.knowledge_reranker_model),
    ):
        requested = value.get(key)
        if requested is not None and requested != configured:
            raise AgentHubError(
                "INVALID_AGENT_CONFIG",
                f"{key} is not supported by the deployed retrieval components.",
                422,
            )
        result[key] = configured
    if result["retrieval_strategy"] not in {item.value for item in RetrievalStrategy}:
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    for key in ("dense_top_k", "sparse_top_k", "candidate_top_k", "final_top_k"):
        item = result[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    for key in _RETRIEVAL_OPTIONAL_KEYS:
        item = value.get(key)
        upper = 64_000 if key == "context_budget_bytes" else 100
        lower = 2 if key == "context_budget_bytes" else 1
        if item is not None and (
            isinstance(item, bool) or not isinstance(item, int) or not lower <= item <= upper
        ):
            raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    projection = result.get("evidence_projection")
    if projection is not None and (
        projection != "compact_v1" or result.get("context_budget_bytes") is None
    ):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    if projection is None:
        result.pop("evidence_projection", None)
    selector = result.get("evidence_set_selector")
    if selector is not None and (
        selector != "onyx_v1"
        or result.get("context_budget_bytes") is None
        or projection is not None
        or result["final_top_k"] > 10
        or result["candidate_top_k"] > 20
    ):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    if selector is None:
        result.pop("evidence_set_selector", None)
    if result["final_top_k"] > result["candidate_top_k"]:
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    if result.get("rerank_top_k") is not None and (
        result["rerank_top_k"] > result["candidate_top_k"]
    ):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The retrieval config is invalid.", 422)
    if "context_budget_bytes" in explicitly_none:
        result.pop("context_budget_bytes", None)
    return result


def _draft_runtime_from_version(value: Mapping[str, Any]) -> dict[str, Any]:
    """Derive editable intent, not client-supplied frozen budget evidence."""
    result = dict(value)
    policy = value.get("context_policy")
    if isinstance(policy, Mapping):
        editable = {
            key: policy[key]
            for key in (
                "policy_version",
                "preset",
                "workspace_input_limit",
                "workspace_input_limit_source",
                "soft_trigger_percent",
                "hard_trigger_percent",
                "target_percent",
                "counter_id",
            )
            if key in policy
        }
        if editable.get("workspace_input_limit_source") in {"UNSET", "DEFAULT"}:
            editable.pop("workspace_input_limit", None)
        result["context_policy"] = editable
    memory = value.get("memory")
    if isinstance(memory, Mapping):
        result["memory"] = dict(memory)
        if memory.get("max_memory_tokens_source") in {"UNSET", "DEFAULT"}:
            result["memory"].pop("max_memory_tokens", None)
    return result


def _validate_runtime_config(
    value: Mapping[str, Any], *, legacy_stored: bool = False
) -> dict[str, Any]:
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
    raw_sources = value.get("context_budget_sources")
    if raw_sources is None:
        raw_sources = {}
    if (
        not isinstance(raw_sources, Mapping)
        or not set(raw_sources).issubset(_CONTEXT_BUDGET_KEYS)
        or any(
            not isinstance(source, str) or source not in _CONFIGURATION_SOURCES
            for source in raw_sources.values()
        )
    ):
        raise AgentHubError(
            "INVALID_AGENT_CONFIG", "The budget configuration sources are invalid.", 422
        )
    supplied_budget = value.get("context_budget", {})
    legacy_default_budget = (
        legacy_stored
        and value.get("context_budget_sources") is None
        and result["context_budget"] == DEFAULT_CONTEXT_BUDGET
    )
    sources = {}
    for key in _CONTEXT_BUDGET_KEYS:
        source = raw_sources.get(
            key, "CUSTOM" if key in supplied_budget and not legacy_default_budget else "UNSET"
        )
        if source == "CUSTOM" and key not in supplied_budget:
            raise AgentHubError("INVALID_AGENT_CONFIG", "A custom budget needs a value.", 422)
        sources[key] = source
    for key in _RUNTIME_SCALAR_KEYS:
        item = result[key]
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 1 <= item <= MAX_RUNTIME_LIMITS[key]
        ):
            raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    cost_limit = result["max_cost_micro_usd"]
    if cost_limit is not None and (
        isinstance(cost_limit, bool)
        or not isinstance(cost_limit, int)
        or not 1 <= cost_limit <= MAX_RUN_COST_LIMIT_MICRO_USD
    ):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    for item in result["context_budget"].values():
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    for key, maximum in MAX_CONTEXT_BUDGET.items():
        if result["context_budget"][key] > maximum:
            raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    for key, source in sources.items():
        if source != "CUSTOM":
            result["context_budget"][key] = DEFAULT_CONTEXT_BUDGET[key]
    result["context_budget_sources"] = sources
    memory = result.get("memory")
    if not isinstance(memory, Mapping) or not set(memory).issubset(_MEMORY_KEYS):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    memory = {**DEFAULT_MEMORY_CONFIG, **dict(memory)}
    if any(
        not isinstance(item, bool) for key, item in memory.items() if key in DEFAULT_MEMORY_CONFIG
    ):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    memory_tokens = memory.get("max_memory_tokens")
    if memory_tokens is not None and (
        isinstance(memory_tokens, bool) or not isinstance(memory_tokens, int) or memory_tokens < 1
    ):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The runtime config is invalid.", 422)
    memory_source = memory.get("max_memory_tokens_source")
    if memory_source is None:
        memory_source = "CUSTOM" if memory_tokens is not None else "UNSET"
    if not isinstance(memory_source, str) or memory_source not in _CONFIGURATION_SOURCES:
        raise AgentHubError(
            "INVALID_AGENT_CONFIG", "The memory configuration source is invalid.", 422
        )
    if (memory_source == "CUSTOM") != (memory_tokens is not None):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The memory source and value conflict.", 422)
    if not memory["long_term_memory"] and memory_tokens is not None:
        raise AgentHubError(
            "INVALID_AGENT_CONFIG", "A memory budget requires long-term memory.", 422
        )
    memory.pop("max_memory_tokens", None)
    memory.pop("max_memory_tokens_source", None)
    if memory["long_term_memory"]:
        memory["max_memory_tokens_source"] = memory_source
        if memory_tokens is not None:
            memory["max_memory_tokens"] = memory_tokens
    # All-false is the same decision as never having had the key, so it is
    # written the same way: absent. Projecting a block of falses would change
    # the canonical JSON of every republished draft and therefore its hash,
    # which is the same reason ``source_kind`` is added conditionally above.
    if any(memory[key] for key in DEFAULT_MEMORY_CONFIG):
        result["memory"] = memory
    else:
        result.pop("memory", None)
    policy_request = value.get("context_policy")
    if policy_request is None:
        result.pop("context_policy", None)
    else:
        try:
            result["context_policy"] = normalize_context_policy_request(policy_request)
        except (TypeError, ValueError):
            raise AgentHubError(
                "INVALID_CONTEXT_POLICY", "The context policy is invalid.", 422
            ) from None
    history_policy = value.get("history_policy")
    if history_policy is None:
        result.pop("history_policy", None)
    elif history_policy != "deterministic_history_v1" or policy_request is None:
        raise AgentHubError(
            "INVALID_CONTEXT_POLICY", "The history policy requires an adaptive context policy.", 422
        )
    else:
        result["history_policy"] = history_policy
    return result


def _resolved_runtime_config(
    runtime_config: Mapping[str, Any],
    resolved_model: tuple[tuple[ResolvedModelProfile, ...], dict[UUID, str]],
) -> dict[str, Any]:
    """Resolve one new publication without reinterpreting numeric defaults.

    Sources are kept in the draft, so an explicit value equal to a historical
    default is still custom. Existing published specs never pass through here.
    """
    validated = _validate_runtime_config(runtime_config, legacy_stored=True)
    sources = validated["context_budget_sources"]
    policy_request = validated.get("context_policy")
    if policy_request is not None:
        model_inputs = [
            {
                **_profile_projection(
                    profile, resolved_model[1][profile.id], include_context_metadata=True
                ),
                "profile_id": str(profile.id),
                "credential_ref": str(profile.provider_credential_id),
            }
            for profile in resolved_model[0]
        ]
        try:
            policy = resolve_context_policy(
                policy_request,
                model_inputs,
                reserved_output_tokens=validated["context_budget"]["reserved_output_tokens"],
            )
        except (TypeError, ValueError):
            raise AgentHubError(
                "INVALID_CONTEXT_POLICY", "The context policy cannot be resolved safely.", 422
            ) from None
        validated["context_policy"] = policy
        derived = derive_context_budget(policy["input_limit_tokens"])
        for key, value in derived.items():
            if sources[key] != "CUSTOM":
                validated["context_budget"][key] = value
        memory = validated.get("memory")
        if isinstance(memory, dict) and memory.get("long_term_memory"):
            if memory["max_memory_tokens_source"] != "CUSTOM":
                memory["max_memory_tokens"] = min(4096, policy["input_limit_tokens"])
        return validated
    windows = [
        profile.capabilities.max_context_tokens
        for profile in resolved_model[0]
        if isinstance(profile.capabilities.max_context_tokens, int)
        and not isinstance(profile.capabilities.max_context_tokens, bool)
    ]
    if windows and not any(source == "CUSTOM" for source in sources.values()):
        validated["context_budget"] = derive_context_budget(min(windows))
    memory = validated.get("memory")
    if isinstance(memory, dict) and memory.get("long_term_memory"):
        if memory.pop("max_memory_tokens_source") != "CUSTOM":
            memory["max_memory_tokens"] = (
                derive_memory_tokens(min(windows)) if windows else DEFAULT_MEMORY_TOKENS
            )
    # Schema 1/2 consumers keep their original runtime shape and default hash.
    validated.pop("context_budget_sources", None)
    return validated


def _validate_published_context_budget(
    runtime_config: Mapping[str, Any],
    resolved_model: tuple[tuple[ResolvedModelProfile, ...], dict[UUID, str]],
) -> None:
    budget = runtime_config.get("context_budget")
    if not isinstance(budget, Mapping):
        raise AgentHubError("INVALID_AGENT_CONFIG", "The context budget is invalid.", 422)
    for profile in resolved_model[0]:
        max_context = profile.capabilities.max_context_tokens
        if not isinstance(max_context, int):
            continue
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value >= max_context
            for value in budget.values()
        ):
            raise AgentHubError(
                "INVALID_AGENT_CONFIG",
                "Each context budget must remain below the model context limit.",
                422,
            )


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
