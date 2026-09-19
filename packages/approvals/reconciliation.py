"""Small, idempotent reconciliation operations for durable approval state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.adapters.langgraph import CheckpointProbe
from packages.agent_runtime.models import AgentRun
from packages.approvals.contracts import ApprovalDecisionStatus
from packages.approvals.models import Approval
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext


class ApprovalReconciliationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        checkpoint_probe: CheckpointProbe | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.checkpoint_probe = checkpoint_probe

    async def reconcile_run(
        self,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        *,
        checkpoint_exists: bool | None = None,
    ) -> AgentRun:
        if "agent_run" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
        if checkpoint_exists is None:
            if self.checkpoint_probe is None:
                raise AgentHubError(
                    "CHECKPOINT_PROBE_NOT_CONFIGURED",
                    "Checkpoint reconciliation is not configured.",
                    503,
                )
            checkpoint_exists = await self.checkpoint_probe.has_checkpoint(
                context.workspace_id, run_id
            )
        async with self.session_factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(AgentRun.workspace_id == UUID(context.workspace_id), AgentRun.id == run_id)
                .with_for_update()
            )
            if run is None:
                raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
            approvals = list(
                await session.scalars(
                    select(Approval).where(
                        Approval.workspace_id == UUID(context.workspace_id),
                        Approval.run_id == run_id,
                    )
                )
            )
            pending = [
                approval
                for approval in approvals
                if approval.decision_status == ApprovalDecisionStatus.PENDING
            ]
            before_status = run.status
            before_failure_code = run.failure_code
            before_completed_at = run.completed_at
            now = datetime.now(UTC)
            expired_count = 0
            for approval in pending:
                if approval.expires_at is not None and approval.expires_at <= now:
                    approval.decision_status = ApprovalDecisionStatus.EXPIRED
                    approval.decided_at = now
                    expired_count += 1
            pending = [
                approval
                for approval in pending
                if approval.decision_status == ApprovalDecisionStatus.PENDING
            ]
            _apply_reconciliation_transition(
                run, pending, checkpoint_exists, approvals=approvals
            )
            if run.status in {"NEEDS_ATTENTION", "CANCELLED"} and run.completed_at is None:
                run.completed_at = now
            if (
                (before_status, before_failure_code) != (run.status, run.failure_code)
                or before_completed_at != run.completed_at
                or expired_count
            ):
                await _append_reconciliation_step(
                    session,
                    run,
                    old_status=before_status,
                    reason=_reconciliation_reason(run, expired_count),
                )
            await session.commit()
            await session.refresh(run)
            return run

    async def reconcile_candidates(
        self,
        *,
        batch_size: int = 100,
        stale_after_seconds: int = 300,
    ) -> dict[str, int]:
        """Reconcile a bounded, tenant-scoped batch for the worker beat task."""

        if self.checkpoint_probe is None:
            raise AgentHubError(
                "CHECKPOINT_PROBE_NOT_CONFIGURED",
                "Checkpoint reconciliation is not configured.",
                503,
            )
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        async with self.session_factory() as session:
            candidates = list(
                await session.scalars(
                    select(AgentRun)
                    .where(
                        AgentRun.status.in_(
                            {"RUNNING", "WAITING_APPROVAL", "CANCEL_REQUESTED", "NEEDS_ATTENTION"}
                        ),
                        AgentRun.started_at <= cutoff,
                    )
                    .order_by(AgentRun.started_at, AgentRun.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
        result = {"scanned": len(candidates), "changed": 0, "errors": 0}
        for candidate in candidates:
            try:
                exists = await self.checkpoint_probe.has_checkpoint(
                    candidate.workspace_id, candidate.id
                )
                async with self.session_factory() as session:
                    run = await session.scalar(
                        select(AgentRun)
                        .where(
                            AgentRun.workspace_id == candidate.workspace_id,
                            AgentRun.id == candidate.id,
                        )
                        .with_for_update()
                    )
                    if run is None:
                        continue
                    before = (run.status, run.failure_code)
                    approvals = list(
                        await session.scalars(
                            select(Approval).where(
                                Approval.workspace_id == run.workspace_id,
                                Approval.run_id == run.id,
                            )
                        )
                    )
                    pending = [
                        approval
                        for approval in approvals
                        if approval.decision_status == ApprovalDecisionStatus.PENDING
                    ]
                    before_status = run.status
                    before_completed_at = run.completed_at
                    now = datetime.now(UTC)
                    expired_count = 0
                    for approval in pending:
                        if approval.expires_at is not None and approval.expires_at <= now:
                            approval.decision_status = ApprovalDecisionStatus.EXPIRED
                            approval.decided_at = now
                            expired_count += 1
                    pending = [
                        approval
                        for approval in pending
                        if approval.decision_status == ApprovalDecisionStatus.PENDING
                    ]
                    _apply_reconciliation_transition(run, pending, exists, approvals=approvals)
                    if run.status in {"NEEDS_ATTENTION", "CANCELLED"} and run.completed_at is None:
                        run.completed_at = now
                    changed = (
                        before != (run.status, run.failure_code)
                        or before_completed_at != run.completed_at
                        or expired_count
                    )
                    if changed:
                        await _append_reconciliation_step(
                            session,
                            run,
                            old_status=before_status,
                            reason=_reconciliation_reason(run, expired_count),
                        )
                        result["changed"] += 1
                    await session.commit()
            except Exception:
                result["errors"] += 1
        return result


def _apply_reconciliation_transition(
    run: AgentRun,
    pending: list[Approval],
    checkpoint_exists: bool,
    *,
    approvals: list[Approval] | None = None,
) -> None:
    if run.status == "RUNNING" and pending:
        if not checkpoint_exists:
            run.status = "NEEDS_ATTENTION"
            run.failure_code = "APPROVAL_CHECKPOINT_MISSING"
        else:
            run.status = "WAITING_APPROVAL"
    if run.status == "WAITING_APPROVAL" and not checkpoint_exists:
        run.status = "NEEDS_ATTENTION"
        run.failure_code = "APPROVAL_CHECKPOINT_MISSING"
    all_approvals = approvals or pending
    if run.status == "CANCEL_REQUESTED" and any(
        approval.execution_status == "CLAIMED" for approval in all_approvals
    ):
        run.status = "NEEDS_ATTENTION"
        run.failure_code = "ACTION_RECONCILIATION_REQUIRED"


async def _append_reconciliation_step(
    session: AsyncSession,
    run: AgentRun,
    *,
    old_status: str,
    reason: str,
) -> None:
    from packages.agent_runtime.models import RunStep

    sequence = await session.scalar(
        select(RunStep.sequence_number)
        .where(RunStep.workspace_id == run.workspace_id, RunStep.agent_run_id == run.id)
        .order_by(desc(RunStep.sequence_number))
        .limit(1)
    )
    session.add(
        RunStep(
            workspace_id=run.workspace_id,
            agent_run_id=run.id,
            sequence_number=int(sequence or 0) + 1,
            kind="RECONCILIATION",
            status=run.status,
            safe_metadata={
                "reason": reason,
                "old_status": old_status,
                "new_status": run.status,
                "failure_code": run.failure_code,
            },
        )
    )


def _reconciliation_reason(run: AgentRun, expired_count: int) -> str:
    if expired_count:
        return "approval_expired"
    if run.failure_code == "APPROVAL_CHECKPOINT_MISSING":
        return "checkpoint_missing"
    if run.failure_code == "ACTION_RECONCILIATION_REQUIRED":
        return "claimed_action_reconciliation"
    if run.status == "WAITING_APPROVAL":
        return "checkpoint_recovered"
    return "terminal_reconciliation"


__all__ = ["ApprovalReconciliationService"]
