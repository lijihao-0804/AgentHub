"""M4-B published-version-bound READ tool execution runtime."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentVersion, ToolRevision
from packages.control_plane.audit import assert_safe_metadata
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.observability import NoopTraceSink
from packages.observability.contracts import TraceSink, TraceSpan
from packages.tools.audit import NoopToolAuditSink, ToolAudit, ToolAuditSink, record_tool_audit
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
    ToolRisk,
)
from packages.tools.errors import ToolHandlerError
from packages.tools.policy import ToolPolicy, ToolPolicyDecision
from packages.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30
_MAX_TIMEOUT_SECONDS = 120
_FORBIDDEN_ARGUMENT_KEYS = frozenset(
    {"workspace_id", "organization_id", "user_id", "tool_revision_id"}
)


def _uuid(value: UUID | str, *, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (AttributeError, ValueError):
        raise AgentHubError("INVALID_TOOL_CONTEXT", f"The {field} is invalid.", 400) from None


def validate_executable_tool_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the minimum runtime schema while keeping old M4-A specs compatible."""

    if not isinstance(spec, Mapping):
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
    required = ("kind", "identity", "input_schema", "effect", "risk_level", "approval_policy")
    if any(key not in spec for key in required):
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
    if spec["kind"] != "builtin" or not isinstance(spec["identity"], str) or not spec["identity"]:
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
    if not isinstance(spec["input_schema"], Mapping):
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
    if spec["effect"] not in {item.value for item in ToolEffect}:
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
    if spec["risk_level"] not in {item.value for item in ToolRisk}:
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
    if spec["approval_policy"] not in {item.value for item in ToolApprovalPolicy}:
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
    timeout = spec.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
    normalized = dict(spec)
    normalized["description"] = spec.get("description", "")
    normalized["timeout_seconds"] = timeout
    if not isinstance(normalized["description"], str):
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)
    try:
        Draft202012Validator.check_schema(dict(spec["input_schema"]))
    except SchemaError:
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422) from None
    return normalized


def _strict_input_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(dict(schema))
    if normalized.get("type") not in (None, "object"):
        raise AgentHubError("TOOL_REVISION_INVALID", "The tool input schema is invalid.", 422)
    normalized["type"] = "object"
    # M4-B arguments are a closed object even when an old revision omitted this keyword.
    normalized["additionalProperties"] = False
    return normalized


def _reject_injected_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        if any(str(key) in _FORBIDDEN_ARGUMENT_KEYS for key in value):
            raise AgentHubError("TOOL_ARGUMENT_INVALID", "The tool arguments are invalid.", 422)
        for child in value.values():
            _reject_injected_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_injected_keys(child)


async def _safe_span_start(
    sink: TraceSink,
    attributes: Mapping[str, Any],
) -> TraceSpan | None:
    try:
        return await sink.start_span("tool.execute", attributes)
    except Exception:
        logger.warning("tool_trace_start_failed")
        return None


async def _safe_span_end(
    span: TraceSpan | None,
    *,
    attributes: Mapping[str, Any],
    status: str,
    failure_code: str | None,
) -> None:
    if span is None:
        return
    try:
        await span.end(attributes=attributes, status=status, failure_code=failure_code)
    except Exception:
        logger.warning("tool_trace_end_failed")


class PublishedToolResolver:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_definitions(
        self,
        *,
        workspace_id: UUID,
        agent_version_id: UUID,
    ) -> tuple[ToolDefinition, ...]:
        """List the exact hash-verified tool revisions published in a version."""

        version = await self.session.scalar(
            select(AgentVersion).where(
                AgentVersion.workspace_id == workspace_id,
                AgentVersion.id == agent_version_id,
            )
        )
        if version is None:
            raise AgentHubError(
                "AGENT_VERSION_NOT_FOUND", "The published agent version was not found.", 404
            )
        if canonical_json_hash(version.resolved_spec) != version.resolved_spec_hash:
            raise AgentHubError(
                "AGENT_VERSION_INTEGRITY_ERROR",
                "The published agent version is invalid.",
                422,
            )
        entries = version.resolved_spec.get("tools", [])
        if not isinstance(entries, list):
            raise AgentHubError(
                "TOOL_REVISION_INTEGRITY_ERROR", "The published tool binding is invalid.", 422
            )
        identities: list[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise AgentHubError(
                    "TOOL_REVISION_INTEGRITY_ERROR", "The published tool binding is invalid.", 422
                )
            try:
                revision_id = UUID(str(entry["tool_revision_id"]))
                revision = await self.session.scalar(
                    select(ToolRevision).where(
                        ToolRevision.workspace_id == workspace_id,
                        ToolRevision.id == revision_id,
                    )
                )
            except (KeyError, ValueError):
                revision = None
            if revision is None or revision.spec_hash != entry.get("tool_spec_hash"):
                raise AgentHubError(
                    "TOOL_REVISION_INTEGRITY_ERROR", "The published tool binding is invalid.", 422
                )
            if canonical_json_hash(revision.spec) != revision.spec_hash:
                raise AgentHubError(
                    "TOOL_REVISION_INTEGRITY_ERROR", "The tool revision is invalid.", 422
                )
            identities.append(validate_executable_tool_spec(revision.spec)["identity"])
        definitions = []
        for identity in dict.fromkeys(identities):
            definitions.append(
                await self.resolve(
                    workspace_id=workspace_id,
                    agent_version_id=agent_version_id,
                    tool_identity=identity,
                )
            )
        return tuple(definitions)

    async def resolve(
        self,
        *,
        workspace_id: UUID,
        agent_version_id: UUID,
        tool_identity: str,
    ) -> ToolDefinition:
        version = await self.session.scalar(
            select(AgentVersion).where(
                AgentVersion.workspace_id == workspace_id,
                AgentVersion.id == agent_version_id,
            )
        )
        if version is None:
            raise AgentHubError(
                "AGENT_VERSION_NOT_FOUND", "The published agent version was not found.", 404
            )
        try:
            if canonical_json_hash(version.resolved_spec) != version.resolved_spec_hash:
                raise AgentHubError(
                    "TOOL_REVISION_INTEGRITY_ERROR",
                    "The published tool binding is invalid.",
                    422,
                )
            tool_entries = version.resolved_spec.get("tools", [])
            retrieval = version.resolved_spec.get("retrieval", {})
            if not isinstance(tool_entries, list) or not isinstance(retrieval, Mapping):
                raise TypeError
        except (AttributeError, TypeError):
            raise AgentHubError(
                "TOOL_REVISION_INTEGRITY_ERROR", "The published tool binding is invalid.", 422
            ) from None

        for entry in tool_entries:
            if not isinstance(entry, Mapping):
                raise AgentHubError(
                    "TOOL_REVISION_INTEGRITY_ERROR", "The published tool binding is invalid.", 422
                )
            revision_id = entry.get("tool_revision_id")
            frozen_hash = entry.get("tool_spec_hash")
            try:
                revision_uuid = UUID(str(revision_id))
            except (AttributeError, ValueError):
                raise AgentHubError(
                    "TOOL_REVISION_INTEGRITY_ERROR", "The published tool binding is invalid.", 422
                ) from None
            revision = await self.session.scalar(
                select(ToolRevision).where(
                    ToolRevision.workspace_id == workspace_id,
                    ToolRevision.id == revision_uuid,
                )
            )
            if revision is None or revision.spec_hash != frozen_hash:
                raise AgentHubError(
                    "TOOL_REVISION_INTEGRITY_ERROR", "The published tool binding is invalid.", 422
                )
            if canonical_json_hash(revision.spec) != revision.spec_hash:
                raise AgentHubError(
                    "TOOL_REVISION_INTEGRITY_ERROR", "The tool revision is invalid.", 422
                )
            spec = validate_executable_tool_spec(revision.spec)
            if spec["identity"] != tool_identity:
                continue
            snapshots = retrieval.get("knowledge_snapshots", [])
            if not isinstance(snapshots, list):
                snapshots = []
            snapshot_refs = tuple(
                {
                    "snapshot_id": str(item["snapshot_id"]),
                    "snapshot_hash": str(item["snapshot_hash"]),
                }
                for item in snapshots
                if isinstance(item, Mapping)
                and item.get("snapshot_id")
                and item.get("snapshot_hash")
            )
            return ToolDefinition(
                identity=spec["identity"],
                revision_id=revision.id,
                spec_hash=revision.spec_hash,
                description=spec["description"],
                input_schema=dict(spec["input_schema"]),
                effect=ToolEffect(spec["effect"]),
                risk_level=ToolRisk(spec["risk_level"]),
                approval_policy=ToolApprovalPolicy(spec["approval_policy"]),
                timeout_seconds=spec["timeout_seconds"],
                snapshot_refs=snapshot_refs,
                retrieval_config=dict(retrieval),
            )
        raise AgentHubError("UNKNOWN_TOOL", "The published tool was not found.", 404)


class PublishedToolCatalog(PublishedToolResolver):
    """Provider-neutral catalog facade for published tool definitions."""

    async def list(
        self,
        *,
        workspace_id: UUID,
        agent_version_id: UUID,
    ) -> tuple[ToolDefinition, ...]:
        return await self.list_definitions(
            workspace_id=workspace_id,
            agent_version_id=agent_version_id,
        )


class ToolRuntime:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ToolRegistry | None = None,
        trace_sink: TraceSink | None = None,
        audit_sink: ToolAuditSink | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry or ToolRegistry()
        self.trace_sink = trace_sink or NoopTraceSink()
        self.audit_sink = audit_sink or NoopToolAuditSink()

    async def execute(
        self,
        *,
        context: WorkspaceExecutionContext,
        agent_version_id: UUID | str,
        tool_identity: str,
        arguments: Mapping[str, Any],
        tool_call_id: str,
    ) -> ToolResult:
        if "tool_run" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
        version_uuid = _uuid(agent_version_id, field="agent_version_id")
        started = time.perf_counter()
        span = await _safe_span_start(
            self.trace_sink,
            {
                "workspace_id": context.workspace_id,
                "agent_version_id": str(version_uuid),
                "tool_identity": tool_identity,
                "argument_keys": sorted(str(key) for key in arguments)
                if isinstance(arguments, Mapping)
                else [],
            },
        )
        definition: ToolDefinition | None = None
        decision: ToolPolicyDecision | None = None
        result: ToolResult
        try:
            if not isinstance(arguments, Mapping):
                result = ToolResult.failure(
                    "TOOL_ARGUMENT_INVALID", "The tool arguments are invalid."
                )
            else:
                async with self.session_factory() as session:
                    try:
                        definition = await PublishedToolResolver(session).resolve(
                            workspace_id=_uuid(context.workspace_id, field="workspace_id"),
                            agent_version_id=version_uuid,
                            tool_identity=tool_identity,
                        )
                        _reject_injected_keys(arguments)
                        validator = Draft202012Validator(
                            _strict_input_schema(definition.input_schema)
                        )
                        validator.validate(dict(arguments))
                    except (ValidationError, SchemaError):
                        result = ToolResult.failure(
                            "TOOL_ARGUMENT_INVALID", "The tool arguments are invalid."
                        )
                    except AgentHubError as exc:
                        result = ToolResult.failure(exc.code, exc.message)
                    else:
                        decision = ToolPolicy.decide(definition)
                        if decision is ToolPolicyDecision.REQUIRE_APPROVAL:
                            result = ToolResult.failure(
                                "TOOL_APPROVAL_NOT_AVAILABLE",
                                "This tool requires approval before execution.",
                            )
                        else:
                            handler = self.registry.resolve(definition.identity)
                            if handler is None:
                                result = ToolResult.failure(
                                    "UNKNOWN_TOOL", "The requested tool is not registered."
                                )
                            else:
                                execution_context = ToolExecutionContext(
                                    workspace_context=context,
                                    agent_version_id=version_uuid,
                                    tool_call_id=tool_call_id,
                                )
                                try:
                                    async with asyncio.timeout(definition.timeout_seconds):
                                        data = await handler(
                                            execution_context, definition, arguments, session
                                        )
                                    result = ToolResult.success(data)
                                except TimeoutError:
                                    result = ToolResult.failure(
                                        "TOOL_TIMEOUT", "The tool execution timed out."
                                    )
                                except ToolHandlerError as exc:
                                    result = ToolResult.failure(exc.code, exc.message)
                                except Exception:
                                    logger.warning("tool_handler_failed", exc_info=True)
                                    result = ToolResult.failure(
                                        "TOOL_EXECUTION_FAILED", "The tool execution failed."
                                    )
        except AgentHubError as exc:
            result = ToolResult.failure(exc.code, exc.message)
        except Exception:
            logger.warning("tool_runtime_failed", exc_info=True)
            result = ToolResult.failure("TOOL_EXECUTION_FAILED", "The tool execution failed.")

        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        result = ToolResult(
            status=result.status,
            data=result.data,
            error_code=result.error_code,
            safe_message=result.safe_message,
            data_trust="UNTRUSTED",
            duration_ms=duration_ms,
        )
        audit = ToolAudit(
            tool_identity=tool_identity,
            tool_revision_id=str(definition.revision_id) if definition else None,
            agent_version_id=str(version_uuid),
            decision=decision.value if decision else None,
            status=result.status.value,
            error_code=result.error_code,
            duration_ms=duration_ms,
            argument_keys=tuple(
                sorted(str(key) for key in arguments) if isinstance(arguments, Mapping) else []
            ),
        )
        metadata = assert_safe_metadata(audit.metadata())
        await record_tool_audit(
            self.audit_sink,
            context,
            agent_version_id=version_uuid,
            tool_call_id=tool_call_id,
            metadata=metadata,
        )
        await _safe_span_end(
            span,
            attributes={
                "workspace_id": context.workspace_id,
                "agent_version_id": str(version_uuid),
                "tool_identity": tool_identity,
                "tool_revision_id": str(definition.revision_id) if definition else None,
                "decision": decision.value if decision else None,
                "status": result.status.value,
                "error_code": result.error_code,
                "duration_ms": duration_ms,
                "argument_keys": metadata["argument_keys"],
            },
            status="ok" if result.status is ToolResultStatus.SUCCESS else "error",
            failure_code=result.error_code,
        )
        return result


__all__ = [
    "PublishedToolCatalog",
    "PublishedToolResolver",
    "ToolRuntime",
    "validate_executable_tool_spec",
]
