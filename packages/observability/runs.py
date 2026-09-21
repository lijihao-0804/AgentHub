"""Safe, provider-neutral query projections for Agent Runtime runs."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun, AgentVersion, RunStep
from packages.approvals.models import Approval
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext

_RUN_STATUSES = frozenset(
    {
        "RUNNING",
        "WAITING_APPROVAL",
        "SUCCEEDED",
        "FAILED",
        "NEEDS_ATTENTION",
        "CANCEL_REQUESTED",
        "CANCELLED",
    }
)

_SAFE_METADATA_KEYS = frozenset(
    {
        "agent_version_id",
        "approval_id",
        "context_limit",
        "decision_status",
        "dropped_exchange_count",
        "duration_ms",
        "error_code",
        "estimated_input_after",
        "estimated_input_before",
        "execution_status",
        "failure_code",
        "safe_failure_message",
        "logical_action_id",
        "model_round",
        "policy_decision",
        "reserved_output",
        "status",
        "step_count",
        "tool_count",
        "tool_identities",
        "tool_identity",
        "truncated",
    }
)

# One nested block is allowed through, and only because its shape is fixed and
# entirely numeric.  ``thread_context`` says how many earlier turns were replayed
# into this run and how many the window left behind; without it the timeline can
# show that context was trimmed but never say what was trimmed.  The inner keys
# are whitelisted separately so that a future producer cannot widen the payload
# by accident, and the value filter below still rejects anything non-scalar.
_SAFE_NESTED_METADATA_KEYS: dict[str, frozenset[str]] = {
    "thread_context": frozenset(
        {
            "thread_id",
            "turns_available",
            "turns_included",
            "turns_dropped_by_window",
            "max_turns",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class RunCursor:
    created_at: datetime
    run_id: UUID


@dataclass(frozen=True, slots=True)
class TimelineSource:
    occurred_at: datetime
    priority: int
    tie_breaker: str
    kind: str
    status: str
    duration_ms: float | None
    metadata: dict[str, Any]
    failure_code: str | None


def classify_failure(failure_code: str | None) -> str | None:
    """Map an internal failure code to a stable presentation category."""

    if not failure_code:
        return None
    code = failure_code.upper()
    if code == "UNKNOWN_OUTCOME" or code.startswith("ACTION_") or code.startswith("TICKET_"):
        return "ACTION"
    if code.startswith(("APPROVAL_", "CHECKPOINT_")):
        return "APPROVAL"
    if code.startswith(("TOOL_", "UNKNOWN_TOOL")):
        return "TOOL"
    if code.startswith(
        (
            "QDRANT",
            "EMBEDDER",
            "SPARSE_",
            "KNOWLEDGE_",
            "RETRIEVAL_",
            "INVALID_DENSE",
            "INVALID_SPARSE",
        )
    ):
        return "KNOWLEDGE"
    if code.startswith(("MODEL_", "AGENT_MODEL")):
        return "MODEL"
    if code.startswith(("AUTH", "ACCESS", "FORBIDDEN", "TENANT", "WORKSPACE")):
        return "AUTH/TENANT"
    if code.startswith(("AGENT_", "CONTEXT_", "DATABASE", "RUN_", "INTERNAL_")):
        return "RUNTIME"
    return "UNKNOWN"


def encode_run_cursor(created_at: datetime, run_id: UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.astimezone(UTC).isoformat(), "run_id": str(run_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_run_cursor(value: str) -> RunCursor:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        created_at = datetime.fromisoformat(payload["created_at"])
        run_id = UUID(payload["run_id"])
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        raise AgentHubError("INVALID_RUN_CURSOR", "The run cursor is invalid.", 422) from exc
    return RunCursor(created_at=created_at, run_id=run_id)


def _workspace_uuid(context: WorkspaceExecutionContext) -> UUID:
    try:
        return UUID(context.workspace_id)
    except (TypeError, ValueError) as exc:
        raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401) from exc


def _require_read(context: WorkspaceExecutionContext) -> UUID:
    if "workspace_read" not in context.permissions:
        raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
    return _workspace_uuid(context)


def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        inner_keys = _SAFE_NESTED_METADATA_KEYS.get(key)
        if inner_keys is not None:
            if isinstance(value, Mapping):
                nested = {
                    inner_key: inner_value
                    for inner_key, inner_value in value.items()
                    if inner_key in inner_keys
                    and (
                        inner_value is None
                        or isinstance(inner_value, (bool, int, float, str))
                    )
                }
                if nested:
                    result[key] = nested
            continue
        if key not in _SAFE_METADATA_KEYS:
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            result[key] = value
        elif isinstance(value, list) and all(
            isinstance(item, (bool, int, float, str)) for item in value
        ):
            result[key] = list(value)
    return result


def _safe_snapshots(snapshots: Any) -> list[dict[str, Any]]:
    if not isinstance(snapshots, list):
        return []
    allowed = {"binding_mode", "knowledge_base_id", "snapshot_hash", "snapshot_id"}
    return [
        {key: value for key, value in item.items() if key in allowed}
        for item in snapshots
        if isinstance(item, Mapping)
    ]


def _duration_ms(started_at: datetime | None, completed_at: datetime | None) -> float | None:
    if started_at is None or completed_at is None:
        return None
    return round(max((completed_at - started_at).total_seconds() * 1000, 0), 3)


def _approval_summary(approvals: list[Approval]) -> dict[str, int]:
    summary = {
        "total": len(approvals),
        "pending": 0,
        "approved": 0,
        "denied": 0,
        "expired": 0,
        "cancelled": 0,
        "not_started": 0,
        "claimed": 0,
        "succeeded": 0,
        "failed": 0,
        "unknown_outcome": 0,
    }
    for approval in approvals:
        decision = str(approval.decision_status).lower()
        execution = str(approval.execution_status).lower()
        if decision in summary:
            summary[decision] += 1
        if execution in summary:
            summary[execution] += 1
    return summary


def _run_projection(
    run: AgentRun,
    *,
    agent_version_number: int,
    approval_summary: dict[str, int],
) -> dict[str, Any]:
    return {
        "id": run.id,
        # A durable, provider-neutral correlation identifier.  It is intentionally not
        # presented as an external Langfuse/OTel URL.
        "trace_id": str(run.id),
        "workspace_id": run.workspace_id,
        "agent_version_id": run.agent_version_id,
        "agent_version_number": agent_version_number,
        "resolved_spec_hash": run.resolved_spec_hash,
        "status": run.status,
        "failure_code": run.failure_code,
        "failure_category": classify_failure(run.failure_code),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "duration_ms": _duration_ms(run.started_at, run.completed_at),
        "model_step_count": run.model_step_count,
        "tool_call_count": run.tool_call_count,
        "total_input_tokens": run.total_input_tokens,
        "total_output_tokens": run.total_output_tokens,
        "total_tokens": run.total_tokens,
        "total_cached_tokens": run.total_cached_tokens,
        "total_cost_amount": run.total_cost_amount,
        "cost_currency": run.cost_currency,
        "cost_is_estimate": run.cost_is_estimate,
        "approval_summary": approval_summary,
    }


def _approval_aggregate() -> Any:
    return (
        select(
            Approval.workspace_id.label("workspace_id"),
            Approval.run_id.label("run_id"),
            func.count(Approval.id).label("approval_total"),
            func.count(Approval.id)
            .filter(Approval.decision_status == "PENDING")
            .label("approval_pending"),
            func.count(Approval.id)
            .filter(Approval.decision_status == "APPROVED")
            .label("approval_approved"),
            func.count(Approval.id)
            .filter(Approval.decision_status == "DENIED")
            .label("approval_denied"),
            func.count(Approval.id)
            .filter(Approval.decision_status == "EXPIRED")
            .label("approval_expired"),
            func.count(Approval.id)
            .filter(Approval.decision_status == "CANCELLED")
            .label("approval_cancelled"),
            func.count(Approval.id)
            .filter(Approval.execution_status == "NOT_STARTED")
            .label("approval_not_started"),
            func.count(Approval.id)
            .filter(Approval.execution_status == "CLAIMED")
            .label("approval_claimed"),
            func.count(Approval.id)
            .filter(Approval.execution_status == "SUCCEEDED")
            .label("approval_succeeded"),
            func.count(Approval.id)
            .filter(Approval.execution_status == "FAILED")
            .label("approval_failed"),
            func.count(Approval.id)
            .filter(Approval.execution_status == "UNKNOWN_OUTCOME")
            .label("approval_unknown_outcome"),
        )
        .group_by(Approval.workspace_id, Approval.run_id)
        .subquery()
    )


def _summary_from_row(row: Any) -> dict[str, int]:
    return {
        "total": int(row.approval_total or 0),
        "pending": int(row.approval_pending or 0),
        "approved": int(row.approval_approved or 0),
        "denied": int(row.approval_denied or 0),
        "expired": int(row.approval_expired or 0),
        "cancelled": int(row.approval_cancelled or 0),
        "not_started": int(row.approval_not_started or 0),
        "claimed": int(row.approval_claimed or 0),
        "succeeded": int(row.approval_succeeded or 0),
        "failed": int(row.approval_failed or 0),
        "unknown_outcome": int(row.approval_unknown_outcome or 0),
    }


def _aggregate_columns(aggregate: Any) -> tuple[Any, ...]:
    return (
        aggregate.c.approval_total,
        aggregate.c.approval_pending,
        aggregate.c.approval_approved,
        aggregate.c.approval_denied,
        aggregate.c.approval_expired,
        aggregate.c.approval_cancelled,
        aggregate.c.approval_not_started,
        aggregate.c.approval_claimed,
        aggregate.c.approval_succeeded,
        aggregate.c.approval_failed,
        aggregate.c.approval_unknown_outcome,
    )


class RunQueryService:
    """Read-only workspace-scoped query service for the M6 Runs product."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def list_runs(
        self,
        context: WorkspaceExecutionContext,
        *,
        status: str | None = None,
        agent_version_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        workspace_id = _require_read(context)
        if status is not None and status not in _RUN_STATUSES:
            raise AgentHubError("INVALID_RUN_STATUS", "The run status is invalid.", 422)
        if not 1 <= limit <= 100:
            raise AgentHubError(
                "INVALID_RUN_LIMIT", "The run limit must be between 1 and 100.", 422
            )

        aggregate = _approval_aggregate()
        statement: Select[Any] = (
            select(AgentRun, AgentVersion.version_number, *_aggregate_columns(aggregate))
            .join(
                AgentVersion,
                and_(
                    AgentVersion.workspace_id == AgentRun.workspace_id,
                    AgentVersion.id == AgentRun.agent_version_id,
                ),
            )
            .outerjoin(
                aggregate,
                and_(
                    aggregate.c.workspace_id == AgentRun.workspace_id,
                    aggregate.c.run_id == AgentRun.id,
                ),
            )
            .where(AgentRun.workspace_id == workspace_id)
        )
        if status is not None:
            statement = statement.where(AgentRun.status == status)
        if agent_version_id is not None:
            statement = statement.where(AgentRun.agent_version_id == agent_version_id)
        if cursor:
            decoded = decode_run_cursor(cursor)
            statement = statement.where(
                or_(
                    AgentRun.created_at < decoded.created_at,
                    and_(AgentRun.created_at == decoded.created_at, AgentRun.id < decoded.run_id),
                )
            )
        statement = statement.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(
            limit + 1
        )

        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        projections = [
            _run_projection(
                row[0],
                agent_version_number=int(row[1]),
                approval_summary=_summary_from_row(row),
            )
            for row in rows
        ]
        next_cursor = None
        if has_more and rows:
            last_run = rows[-1][0]
            next_cursor = encode_run_cursor(last_run.created_at, last_run.id)
        return projections, next_cursor

    async def get_detail(self, context: WorkspaceExecutionContext, run_id: UUID) -> dict[str, Any]:
        workspace_id = _require_read(context)
        aggregate = _approval_aggregate()
        statement = (
            select(AgentRun, AgentVersion.version_number, *_aggregate_columns(aggregate))
            .join(
                AgentVersion,
                and_(
                    AgentVersion.workspace_id == AgentRun.workspace_id,
                    AgentVersion.id == AgentRun.agent_version_id,
                ),
            )
            .outerjoin(
                aggregate,
                and_(
                    aggregate.c.workspace_id == AgentRun.workspace_id,
                    aggregate.c.run_id == AgentRun.id,
                ),
            )
            .where(AgentRun.workspace_id == workspace_id, AgentRun.id == run_id)
        )
        async with self.session_factory() as session:
            row = (await session.execute(statement)).first()
            if row is None:
                raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
            run = row[0]
            detail = _run_projection(
                run,
                agent_version_number=int(row[1]),
                approval_summary=_summary_from_row(row),
            )
            detail["effective_knowledge_snapshots"] = _safe_snapshots(
                run.effective_knowledge_snapshots
            )
            detail["trace_url"] = None
            return detail

    async def get_timeline(
        self, context: WorkspaceExecutionContext, run_id: UUID
    ) -> list[dict[str, Any]]:
        workspace_id = _require_read(context)
        async with self.session_factory() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.workspace_id == workspace_id, AgentRun.id == run_id)
            )
            if run is None:
                raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
            steps = list(
                await session.scalars(
                    select(RunStep)
                    .where(RunStep.workspace_id == workspace_id, RunStep.agent_run_id == run_id)
                    .order_by(RunStep.sequence_number, RunStep.created_at, RunStep.id)
                )
            )
            approvals = list(
                await session.scalars(
                    select(Approval)
                    .where(Approval.workspace_id == workspace_id, Approval.run_id == run_id)
                    .order_by(Approval.created_at, Approval.id)
                )
            )
        sources = [
            TimelineSource(
                occurred_at=run.created_at,
                priority=0,
                tie_breaker=str(run.id),
                kind="RUN_STARTED",
                status="RUNNING",
                duration_ms=None,
                metadata={"agent_version_id": str(run.agent_version_id)},
                failure_code=None,
            )
        ]
        has_approval_wait = False
        for step in steps:
            mapped = _map_step(step)
            if mapped is None:
                continue
            if mapped.kind == "APPROVAL_WAIT":
                has_approval_wait = True
            sources.append(mapped)
        for approval in approvals:
            if not has_approval_wait:
                sources.append(
                    TimelineSource(
                        occurred_at=approval.created_at,
                        priority=20,
                        tie_breaker=str(approval.id),
                        kind="APPROVAL_WAIT",
                        status=str(approval.decision_status),
                        duration_ms=None,
                        metadata={
                            "approval_id": str(approval.id),
                            "tool_identity": approval.tool_identity,
                            "decision_status": str(approval.decision_status),
                            "execution_status": str(approval.execution_status),
                        },
                        failure_code=None,
                    )
                )
            if approval.decided_at is not None:
                sources.append(
                    TimelineSource(
                        occurred_at=approval.decided_at,
                        priority=30,
                        tie_breaker=str(approval.id),
                        kind="APPROVAL_DECISION",
                        status=str(approval.decision_status),
                        duration_ms=round(
                            max(
                                (approval.decided_at - approval.created_at).total_seconds() * 1000,
                                0,
                            ),
                            3,
                        ),
                        metadata={
                            "approval_id": str(approval.id),
                            "decision_status": str(approval.decision_status),
                        },
                        failure_code=(
                            f"APPROVAL_{approval.decision_status}"
                            if str(approval.decision_status) in {"DENIED", "EXPIRED"}
                            else None
                        ),
                    )
                )
            if approval.executed_at is not None or str(approval.execution_status) != "NOT_STARTED":
                occurred_at = approval.executed_at or approval.updated_at
                sources.append(
                    TimelineSource(
                        occurred_at=occurred_at,
                        priority=40,
                        tie_breaker=str(approval.id),
                        kind="ACTION_EXECUTION",
                        status=str(approval.execution_status),
                        duration_ms=(
                            round(
                                max((occurred_at - approval.created_at).total_seconds() * 1000, 0),
                                3,
                            )
                            if occurred_at is not None
                            else None
                        ),
                        metadata={
                            "approval_id": str(approval.id),
                            "execution_status": str(approval.execution_status),
                            "safe_failure_message": approval.safe_failure_message,
                        },
                        failure_code=approval.failure_code,
                    )
                )
        if run.failure_code and not any(source.failure_code for source in sources):
            sources.append(
                TimelineSource(
                    occurred_at=run.completed_at or run.created_at,
                    priority=50,
                    tie_breaker=str(run.id),
                    kind="FAILURE",
                    status=run.status,
                    duration_ms=None,
                    metadata={},
                    failure_code=run.failure_code,
                )
            )
        sources.sort(key=lambda source: (source.occurred_at, source.priority, source.tie_breaker))
        return [
            {
                "sequence": index,
                "occurred_at": source.occurred_at,
                "kind": source.kind,
                "status": source.status,
                "duration_ms": source.duration_ms,
                "metadata": _safe_metadata(source.metadata),
                "failure_code": source.failure_code,
                "failure_category": classify_failure(source.failure_code),
            }
            for index, source in enumerate(sources, start=1)
        ]


def _map_step(step: RunStep) -> TimelineSource | None:
    metadata = _safe_metadata(step.safe_metadata)
    failure_code = metadata.get("error_code") or metadata.get("failure_code")
    tool_identities = metadata.get("tool_identities")
    is_retrieval = (
        step.kind in {"TOOL_PROPOSAL", "TOOL_EXECUTE", "OBSERVATION"}
        and isinstance(tool_identities, list)
        and any(
            isinstance(identity, str)
            and (identity == "search_knowledge" or identity.startswith("knowledge."))
            for identity in tool_identities
        )
    )
    kind_map = {
        # PREPARE used to be dropped here, which made the one step that knows how
        # much conversation history entered the run invisible to every reader.
        "PREPARE": "FAILURE" if step.status == "FAILED" else "PREPARE",
        "MODEL": "MODEL",
        "TOOL_PROPOSAL": "TOOL",
        "TOOL_EXECUTE": "TOOL",
        "OBSERVATION": "TOOL",
        "APPROVAL_WAIT": "APPROVAL_WAIT",
        "ACTION": "ACTION_EXECUTION",
        "RECONCILIATION": "RESUME",
        "FINISH": "FINISH" if step.status == "SUCCEEDED" else "FAILURE",
        "GUARD": "FAILURE" if step.status == "FAILED" else None,
        "POLICY": "FAILURE" if step.status == "FAILED" else None,
    }
    kind = kind_map.get(step.kind)
    if is_retrieval:
        kind = "RETRIEVAL"
    if kind is None:
        return None
    duration = metadata.get("duration_ms")
    return TimelineSource(
        occurred_at=step.created_at,
        priority={"APPROVAL_WAIT": 20, "ACTION_EXECUTION": 40, "FINISH": 50}.get(kind, 10),
        tie_breaker=f"{step.sequence_number:012d}:{step.id}",
        kind=kind,
        status=step.status,
        duration_ms=float(duration) if isinstance(duration, (int, float)) else None,
        metadata=metadata,
        failure_code=str(failure_code) if failure_code else None,
    )


__all__ = [
    "RunQueryService",
    "classify_failure",
    "decode_run_cursor",
    "encode_run_cursor",
]
