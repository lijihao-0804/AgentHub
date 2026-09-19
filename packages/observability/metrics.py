"""Workspace-scoped PostgreSQL metrics for the M6 observability dashboard."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.approvals.models import Approval
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.observability.runs import _require_read, classify_failure

_FINISHED = ("SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_ATTENTION")
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
_MAX_WINDOW = timedelta(days=90)
_DEFAULT_WINDOW = timedelta(days=7)
_CURRENCIES = frozenset({"USD", "EUR", "CNY", "GBP", "JPY", "CAD", "AUD", "CHF"})


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise AgentHubError(
            "INVALID_OBSERVABILITY_WINDOW", "Window timestamps must include UTC.", 422
        )
    return value.astimezone(UTC)


def normalize_window(
    start: datetime | None,
    end: datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Resolve a bounded UTC window without querying an unbounded history."""

    resolved_end = _utc(end) if end is not None else _utc(now or datetime.now(UTC))
    resolved_start = _utc(start) if start is not None else resolved_end - _DEFAULT_WINDOW
    if resolved_end <= resolved_start:
        raise AgentHubError(
            "INVALID_OBSERVABILITY_WINDOW", "The window must have a positive duration.", 422
        )
    if resolved_end - resolved_start > _MAX_WINDOW:
        raise AgentHubError(
            "INVALID_OBSERVABILITY_WINDOW", "The window cannot exceed 90 days.", 422
        )
    return resolved_start, resolved_end


def _duration_ms_expr() -> Any:
    return func.extract("epoch", AgentRun.completed_at - AgentRun.started_at) * 1000


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _float(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 6) if denominator else None,
    }


def _run_filters(
    workspace_id: UUID,
    start: datetime,
    end: datetime,
    agent_version_id: UUID | None,
) -> list[Any]:
    filters: list[Any] = [
        AgentRun.workspace_id == workspace_id,
        AgentRun.started_at >= start,
        AgentRun.started_at < end,
    ]
    if agent_version_id is not None:
        filters.append(AgentRun.agent_version_id == agent_version_id)
    return filters


def _cost_currency(value: Any) -> str | None:
    if value is None:
        return None
    currency = str(value).upper()
    return currency if currency in _CURRENCIES else currency[:3]


class MetricsQueryService:
    """Read-only, tenant-scoped aggregation service for M6-B."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def summary(
        self,
        context: WorkspaceExecutionContext,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        agent_version_id: UUID | None = None,
    ) -> dict[str, Any]:
        workspace_id = _require_read(context)
        window_start, window_end = normalize_window(start, end)
        filters = _run_filters(workspace_id, window_start, window_end, agent_version_id)
        duration = _duration_ms_expr()

        run_statement: Select[Any] = select(
            func.count(AgentRun.id).filter(AgentRun.status == "SUCCEEDED").label("succeeded"),
            func.count(AgentRun.id).filter(AgentRun.status == "FAILED").label("failed"),
            func.count(AgentRun.id).filter(AgentRun.status == "CANCELLED").label("cancelled"),
            func.count(AgentRun.id)
            .filter(AgentRun.status == "NEEDS_ATTENTION")
            .label("needs_attention"),
            func.count(AgentRun.id).filter(AgentRun.status.in_(_FINISHED)).label("finished"),
            func.percentile_cont(0.5)
            .within_group(duration)
            .filter(AgentRun.completed_at.is_not(None))
            .label("p50_duration"),
            func.percentile_cont(0.95)
            .within_group(duration)
            .filter(AgentRun.completed_at.is_not(None))
            .label("p95_duration"),
            func.avg(duration).filter(AgentRun.completed_at.is_not(None)).label("avg_duration"),
            func.count(AgentRun.id)
            .filter(AgentRun.completed_at.is_not(None))
            .label("latency_sample_count"),
            func.avg(AgentRun.total_tokens)
            .filter(AgentRun.total_tokens.is_not(None))
            .label("avg_tokens"),
            func.avg(AgentRun.total_input_tokens)
            .filter(AgentRun.total_input_tokens.is_not(None))
            .label("avg_input_tokens"),
            func.avg(AgentRun.total_output_tokens)
            .filter(AgentRun.total_output_tokens.is_not(None))
            .label("avg_output_tokens"),
            func.avg(AgentRun.total_cached_tokens)
            .filter(AgentRun.total_cached_tokens.is_not(None))
            .label("avg_cached_tokens"),
            func.sum(AgentRun.total_tokens)
            .filter(AgentRun.total_tokens.is_not(None))
            .label("total_tokens"),
            func.count(AgentRun.id)
            .filter(AgentRun.total_tokens.is_not(None))
            .label("known_usage_count"),
            func.count(AgentRun.id)
            .filter(AgentRun.total_tokens.is_(None))
            .label("unknown_usage_count"),
        ).where(*filters)

        current_run_statement = select(
            func.count(AgentRun.id).filter(AgentRun.status == "RUNNING").label("running"),
            func.count(AgentRun.id)
            .filter(AgentRun.status == "WAITING_APPROVAL")
            .label("waiting_approval"),
            func.count(AgentRun.id)
            .filter(AgentRun.status == "CANCEL_REQUESTED")
            .label("cancel_requested"),
            func.count(AgentRun.id)
            .filter(AgentRun.status == "NEEDS_ATTENTION")
            .label("needs_attention"),
        ).where(AgentRun.workspace_id == workspace_id)
        if agent_version_id is not None:
            current_run_statement = current_run_statement.where(
                AgentRun.agent_version_id == agent_version_id
            )

        cost_statement = select(
            AgentRun.cost_currency.label("currency"),
            func.sum(AgentRun.total_cost_amount).label("total_cost"),
            func.avg(AgentRun.total_cost_amount).label("avg_cost"),
            func.count(AgentRun.id).label("known_cost_count"),
            func.sum(AgentRun.total_cost_amount)
            .filter(AgentRun.status == "SUCCEEDED")
            .label("successful_cost"),
            func.count(AgentRun.id)
            .filter(AgentRun.status == "SUCCEEDED")
            .label("successful_cost_count"),
            func.count(AgentRun.id)
            .filter(AgentRun.cost_is_estimate.is_(True))
            .label("estimated_count"),
            func.count(AgentRun.id)
            .filter(AgentRun.cost_is_estimate.is_(False))
            .label("exact_count"),
        ).where(
            *filters, AgentRun.total_cost_amount.is_not(None), AgentRun.cost_currency.is_not(None)
        )
        cost_statement = cost_statement.group_by(AgentRun.cost_currency).order_by(
            AgentRun.cost_currency
        )

        approval_filters = [
            Approval.workspace_id == workspace_id,
            Approval.run_id == AgentRun.id,
            AgentRun.started_at >= window_start,
            AgentRun.started_at < window_end,
        ]
        if agent_version_id is not None:
            approval_filters.append(AgentRun.agent_version_id == agent_version_id)
        approval_wait = func.extract("epoch", Approval.decided_at - Approval.created_at) * 1000
        approval_statement = (
            select(
                func.count(Approval.id).label("total"),
                func.count(Approval.id)
                .filter(Approval.decision_status == "PENDING")
                .label("pending"),
                func.count(Approval.id)
                .filter(Approval.decision_status == "APPROVED")
                .label("approved"),
                func.count(Approval.id)
                .filter(Approval.decision_status == "DENIED")
                .label("denied"),
                func.count(Approval.id)
                .filter(Approval.decision_status == "EXPIRED")
                .label("expired"),
                func.count(Approval.id)
                .filter(Approval.decision_status == "CANCELLED")
                .label("cancelled"),
                func.count(Approval.id)
                .filter(Approval.execution_status == "NOT_STARTED")
                .label("execution_not_started"),
                func.count(Approval.id)
                .filter(Approval.execution_status == "CLAIMED")
                .label("claimed"),
                func.count(Approval.id)
                .filter(Approval.execution_status == "SUCCEEDED")
                .label("succeeded"),
                func.count(Approval.id)
                .filter(Approval.execution_status == "FAILED")
                .label("failed"),
                func.count(Approval.id)
                .filter(Approval.execution_status == "UNKNOWN_OUTCOME")
                .label("unknown_outcome"),
                func.percentile_cont(0.5)
                .within_group(approval_wait)
                .filter(Approval.decided_at.is_not(None))
                .label("p50_wait"),
                func.percentile_cont(0.95)
                .within_group(approval_wait)
                .filter(Approval.decided_at.is_not(None))
                .label("p95_wait"),
                func.count(Approval.id)
                .filter(Approval.decided_at.is_not(None))
                .label("wait_sample_count"),
            )
            .select_from(Approval)
            .join(AgentRun, AgentRun.id == Approval.run_id)
            .where(*approval_filters)
        )

        unknown_statement = (
            select(func.count(Approval.id))
            .select_from(Approval)
            .join(
                AgentRun,
                and_(
                    AgentRun.workspace_id == Approval.workspace_id, AgentRun.id == Approval.run_id
                ),
            )
            .where(
                Approval.workspace_id == workspace_id,
                Approval.execution_status == "UNKNOWN_OUTCOME",
            )
        )
        if agent_version_id is not None:
            unknown_statement = unknown_statement.where(
                AgentRun.agent_version_id == agent_version_id
            )

        async with self.session_factory() as session:
            run_row = (await session.execute(run_statement)).one()
            current_row = (await session.execute(current_run_statement)).one()
            cost_rows = (await session.execute(cost_statement)).all()
            approval_row = (await session.execute(approval_statement)).one()
            unknown_count = int((await session.execute(unknown_statement)).scalar_one() or 0)

        succeeded = int(run_row.succeeded or 0)
        failed = int(run_row.failed or 0)
        cancelled = int(run_row.cancelled or 0)
        needs_attention = int(run_row.needs_attention or 0)
        finished = int(run_row.finished or 0)
        currency_metrics: list[dict[str, Any]] = []
        for row in cost_rows:
            currency = _cost_currency(row.currency)
            if currency is None:
                continue
            successful_count = int(row.successful_cost_count or 0)
            successful_total = _decimal(row.successful_cost)
            currency_metrics.append(
                {
                    "currency": currency,
                    "total_cost": _decimal(row.total_cost),
                    "avg_cost_per_run": _decimal(row.avg_cost),
                    "cost_per_successful_run": (
                        successful_total / successful_count if successful_count else None
                    ),
                    "successful_cost_denominator": successful_count,
                    "known_cost_count": int(row.known_cost_count or 0),
                    "estimated_count": int(row.estimated_count or 0),
                    "exact_count": int(row.exact_count or 0),
                }
            )

        single_currency = currency_metrics[0] if len(currency_metrics) == 1 else None
        return {
            "window": {"from": window_start, "to": window_end},
            "finished_runs": {
                "succeeded": succeeded,
                "failed": failed,
                "cancelled": cancelled,
                "needs_attention": needs_attention,
                "denominator": finished,
            },
            "success_rate": _rate(succeeded, finished),
            "failure_rate": _rate(failed, finished),
            "cancelled_rate": _rate(cancelled, finished),
            "needs_attention_rate": _rate(needs_attention, finished),
            "latency": {
                "p50_ms": _float(run_row.p50_duration),
                "p95_ms": _float(run_row.p95_duration),
                "avg_ms": _float(run_row.avg_duration),
                "sample_count": int(run_row.latency_sample_count or 0),
            },
            "usage": {
                "avg_tokens_per_run": _float(run_row.avg_tokens),
                "avg_input_tokens": _float(run_row.avg_input_tokens),
                "avg_output_tokens": _float(run_row.avg_output_tokens),
                "avg_cached_tokens": _float(run_row.avg_cached_tokens),
                "total_tokens": int(run_row.total_tokens)
                if run_row.total_tokens is not None
                else None,
                "known_usage_count": int(run_row.known_usage_count or 0),
                "unknown_usage_count": int(run_row.unknown_usage_count or 0),
            },
            "cost": {
                "currency": single_currency["currency"] if single_currency else None,
                "total_cost": single_currency["total_cost"] if single_currency else None,
                "avg_cost_per_run": single_currency["avg_cost_per_run"]
                if single_currency
                else None,
                "cost_per_successful_run": (
                    single_currency["cost_per_successful_run"] if single_currency else None
                ),
                "successful_cost_denominator": (
                    single_currency["successful_cost_denominator"] if single_currency else 0
                ),
                "estimated_count": sum(item["estimated_count"] for item in currency_metrics),
                "exact_count": sum(item["exact_count"] for item in currency_metrics),
                "mixed_currency": len(currency_metrics) > 1,
                "currencies": currency_metrics,
            },
            "current": {
                "running_count": int(current_row.running or 0),
                "waiting_approval_count": int(current_row.waiting_approval or 0),
                "cancel_requested_count": int(current_row.cancel_requested or 0),
                "needs_attention_count": int(current_row.needs_attention or 0),
                "unknown_outcome_action_count": unknown_count,
            },
            "approvals": {
                "approval_total": int(approval_row.total or 0),
                "pending": int(approval_row.pending or 0),
                "approved": int(approval_row.approved or 0),
                "denied": int(approval_row.denied or 0),
                "expired": int(approval_row.expired or 0),
                "cancelled": int(approval_row.cancelled or 0),
                "execution_not_started": int(approval_row.execution_not_started or 0),
                "claimed": int(approval_row.claimed or 0),
                "succeeded": int(approval_row.succeeded or 0),
                "failed": int(approval_row.failed or 0),
                "unknown_outcome": int(approval_row.unknown_outcome or 0),
                "wait_latency": {
                    "p50_ms": _float(approval_row.p50_wait),
                    "p95_ms": _float(approval_row.p95_wait),
                    "sample_count": int(approval_row.wait_sample_count or 0),
                },
            },
        }

    async def failure_analytics(
        self,
        context: WorkspaceExecutionContext,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        category: str | None = None,
        limit: int = 50,
        agent_version_id: UUID | None = None,
    ) -> dict[str, Any]:
        workspace_id = _require_read(context)
        window_start, window_end = normalize_window(start, end)
        if not 1 <= limit <= 100:
            raise AgentHubError(
                "INVALID_OBSERVABILITY_LIMIT", "The limit must be between 1 and 100.", 422
            )
        normalized_category = category.upper() if category else None
        allowed_categories = {
            "MODEL",
            "KNOWLEDGE",
            "TOOL",
            "APPROVAL",
            "ACTION",
            "RUNTIME",
            "AUTH/TENANT",
            "UNKNOWN",
        }
        if normalized_category and normalized_category not in allowed_categories:
            raise AgentHubError("INVALID_FAILURE_CATEGORY", "The failure category is invalid.", 422)
        filters = _run_filters(workspace_id, window_start, window_end, agent_version_id)
        code_statement = (
            select(AgentRun.failure_code, func.count(AgentRun.id).label("count"))
            .where(*filters, AgentRun.failure_code.is_not(None))
            .group_by(AgentRun.failure_code)
            .order_by(func.count(AgentRun.id).desc(), AgentRun.failure_code)
            .limit(1000)
        )
        approval_join = and_(
            Approval.workspace_id == AgentRun.workspace_id,
            Approval.run_id == AgentRun.id,
            Approval.execution_status == "UNKNOWN_OUTCOME",
        )
        failure_statement = (
            select(
                AgentRun,
                AgentVersion.version_number,
                Approval.id.label("approval_id"),
                Approval.logical_action_id,
                Approval.tool_identity,
                Approval.execution_status,
                Approval.failure_code.label("action_failure_code"),
            )
            .join(
                AgentVersion,
                and_(
                    AgentVersion.workspace_id == AgentRun.workspace_id,
                    AgentVersion.id == AgentRun.agent_version_id,
                ),
            )
            .outerjoin(Approval, approval_join)
            .where(*filters, AgentRun.failure_code.is_not(None))
            .order_by(AgentRun.completed_at.desc().nullslast(), AgentRun.started_at.desc())
            .limit(min(500, limit * 5))
        )
        async with self.session_factory() as session:
            code_rows = (await session.execute(code_statement)).all()
            failure_rows = (await session.execute(failure_statement)).all()

        category_counts: dict[str, int] = defaultdict(int)
        top_codes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        total_failures = sum(int(row.count or 0) for row in code_rows)
        for row in code_rows:
            failure_category = classify_failure(row.failure_code) or "UNKNOWN"
            count = int(row.count or 0)
            category_counts[failure_category] += count
            if len(top_codes[failure_category]) < 10:
                top_codes[failure_category].append(
                    {"failure_code": row.failure_code, "count": count}
                )

        items: list[dict[str, Any]] = []
        seen: set[UUID] = set()
        for row in failure_rows:
            run = row[0]
            failure_category = classify_failure(run.failure_code) or "UNKNOWN"
            if normalized_category and failure_category != normalized_category:
                continue
            if run.id in seen:
                continue
            seen.add(run.id)
            items.append(
                {
                    "run_id": run.id,
                    "agent_version_id": run.agent_version_id,
                    "agent_version_number": int(row[1]),
                    "status": run.status,
                    "failure_category": failure_category,
                    "failure_code": run.failure_code,
                    "started_at": run.started_at,
                    "completed_at": run.completed_at,
                    "duration_ms": (
                        round(max((run.completed_at - run.started_at).total_seconds() * 1000, 0), 3)
                        if run.completed_at is not None
                        else None
                    ),
                    "tool_call_count": run.tool_call_count,
                    "approval_id": row.approval_id,
                    "logical_action_id": row.logical_action_id,
                    "tool_identity": row.tool_identity,
                    "execution_status": row.execution_status,
                    "action_failure_code": row.action_failure_code,
                }
            )
            if len(items) >= limit:
                break

        categories = [
            {
                "failure_category": name,
                "count": count,
                "percentage": round(count / total_failures, 6) if total_failures else None,
                "top_failure_codes": top_codes.get(name, []),
            }
            for name, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
        ]
        return {
            "window": {"from": window_start, "to": window_end},
            "total_failure_runs": total_failures,
            "categories": categories,
            "items": items,
        }

    async def agent_version_breakdown(
        self,
        context: WorkspaceExecutionContext,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        workspace_id = _require_read(context)
        window_start, window_end = normalize_window(start, end)
        duration = _duration_ms_expr()
        statement = (
            select(
                AgentVersion.id,
                AgentVersion.version_number,
                AgentRun.cost_currency,
                func.count(AgentRun.id).label("run_count"),
                func.count(AgentRun.id)
                .filter(AgentRun.status == "SUCCEEDED")
                .label("success_count"),
                func.count(AgentRun.id).filter(AgentRun.status == "FAILED").label("failed_count"),
                func.count(AgentRun.id)
                .filter(AgentRun.status == "NEEDS_ATTENTION")
                .label("needs_attention_count"),
                func.percentile_cont(0.95)
                .within_group(duration)
                .filter(AgentRun.completed_at.is_not(None))
                .label("p95_latency"),
                func.avg(AgentRun.total_tokens)
                .filter(AgentRun.total_tokens.is_not(None))
                .label("avg_tokens"),
                func.sum(AgentRun.total_cost_amount)
                .filter(AgentRun.total_cost_amount.is_not(None))
                .label("cost"),
            )
            .join(
                AgentRun,
                and_(
                    AgentRun.workspace_id == AgentVersion.workspace_id,
                    AgentRun.agent_version_id == AgentVersion.id,
                ),
            )
            .where(
                AgentVersion.workspace_id == workspace_id,
                AgentRun.started_at >= window_start,
                AgentRun.started_at < window_end,
            )
            .group_by(AgentVersion.id, AgentVersion.version_number, AgentRun.cost_currency)
            .order_by(AgentVersion.version_number)
        )
        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()
        merged: dict[UUID, dict[str, Any]] = {}
        for row in rows:
            item = merged.setdefault(
                row.id,
                {
                    "agent_version_id": row.id,
                    "version_number": int(row.version_number),
                    "run_count": int(row.run_count or 0),
                    "success_count": int(row.success_count or 0),
                    "failed_count": int(row.failed_count or 0),
                    "needs_attention_count": int(row.needs_attention_count or 0),
                    "p95_latency_ms": _float(row.p95_latency),
                    "avg_tokens": _float(row.avg_tokens),
                    "cost_by_currency": [],
                },
            )
            currency = _cost_currency(row.cost_currency)
            if currency and row.cost is not None:
                item["cost_by_currency"].append(
                    {"currency": currency, "total_cost": _decimal(row.cost)}
                )
        return {"window": {"from": window_start, "to": window_end}, "items": list(merged.values())}

    async def timeseries(
        self,
        context: WorkspaceExecutionContext,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        bucket: str = "day",
        agent_version_id: UUID | None = None,
    ) -> dict[str, Any]:
        workspace_id = _require_read(context)
        window_start, window_end = normalize_window(start, end)
        if bucket not in {"day", "hour"}:
            raise AgentHubError("INVALID_OBSERVABILITY_BUCKET", "Bucket must be day or hour.", 422)
        if bucket == "hour" and window_end - window_start > timedelta(days=2):
            raise AgentHubError(
                "INVALID_OBSERVABILITY_BUCKET", "Hourly data is limited to 48 hours.", 422
            )
        filters = _run_filters(workspace_id, window_start, window_end, agent_version_id)
        bucket_expr = func.date_trunc(bucket, AgentRun.started_at).label("bucket")
        statement = (
            select(
                bucket_expr,
                func.count(AgentRun.id).label("runs"),
                func.count(AgentRun.id).filter(AgentRun.status == "SUCCEEDED").label("succeeded"),
                func.count(AgentRun.id).filter(AgentRun.status == "FAILED").label("failed"),
                func.count(AgentRun.id)
                .filter(AgentRun.status == "NEEDS_ATTENTION")
                .label("needs_attention"),
                func.sum(AgentRun.total_tokens)
                .filter(AgentRun.total_tokens.is_not(None))
                .label("tokens"),
            )
            .where(*filters)
            .group_by(bucket_expr)
            .order_by(bucket_expr)
        )
        cost_bucket = func.date_trunc(bucket, AgentRun.started_at).label("bucket")
        cost_statement = (
            select(
                cost_bucket,
                AgentRun.cost_currency,
                func.sum(AgentRun.total_cost_amount).label("cost"),
            )
            .where(
                *filters,
                AgentRun.cost_currency.is_not(None),
                AgentRun.total_cost_amount.is_not(None),
            )
            .group_by(cost_bucket, AgentRun.cost_currency)
            .order_by(cost_bucket, AgentRun.cost_currency)
        )
        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()
            cost_rows = (await session.execute(cost_statement)).all()
        costs: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
        for row in cost_rows:
            currency = _cost_currency(row.cost_currency)
            if currency:
                costs[row.bucket].append({"currency": currency, "total_cost": _decimal(row.cost)})
        return {
            "window": {"from": window_start, "to": window_end},
            "bucket": bucket,
            "items": [
                {
                    "bucket": row.bucket,
                    "runs": int(row.runs or 0),
                    "succeeded": int(row.succeeded or 0),
                    "failed": int(row.failed or 0),
                    "needs_attention": int(row.needs_attention or 0),
                    "tokens": int(row.tokens) if row.tokens is not None else None,
                    "cost_by_currency": costs.get(row.bucket, []),
                }
                for row in rows
            ],
        }


__all__ = ["MetricsQueryService", "normalize_window"]
