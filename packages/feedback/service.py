"""Immutable feedback content, optimistic review and atomic idempotent DEV imports."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.models import AgentRun
from packages.control_plane.audit import append_audit
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.models import EvaluationDatasetItem, EvaluationDatasetVersion
from packages.evaluation.service import EvaluationDatasetService
from packages.feedback.models import RunFeedback
from packages.threads.models import ThreadTurn

CATEGORIES = frozenset({"FACTUAL", "RETRIEVAL", "TOOL", "LATENCY", "EXPRESSION", "OTHER"})
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_ATTENTION"})


def require(context: WorkspaceExecutionContext, *permissions: str) -> UUID:
    if not context.user_id or any(p not in context.permissions for p in permissions):
        raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
    return UUID(context.workspace_id)


def audit(session, context, feedback, action):
    append_audit(
        session,
        action=action,
        resource_type="run_feedback",
        request_id=context.request_id,
        actor_user_id=UUID(context.user_id),
        workspace_id=feedback.workspace_id,
        organization_id=UUID(context.organization.organization_id),
        resource_id=str(feedback.id),
        safe_metadata={
            "run_id": str(feedback.run_id),
            "content_revision": feedback.content_revision,
            "review_version": feedback.review_version,
            "status": feedback.status,
        },
    )


class FeedbackService:
    async def _run(self, session, context, run_id):
        workspace = require(context, "workspace_read")
        run = await session.scalar(
            select(AgentRun).where(AgentRun.workspace_id == workspace, AgentRun.id == run_id)
        )
        if run is None:
            raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
        return run

    async def submit(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        *,
        run_id: UUID,
        turn_id: UUID | None,
        client_key: str,
        rating: int,
        category: str,
        comment: str = "",
        corrected_answer: str | None = None,
    ) -> RunFeedback:
        run = await self._run(session, context, run_id)
        if run.status not in TERMINAL:
            raise AgentHubError("FEEDBACK_RUN_NOT_TERMINAL", "Wait for the run to finish.", 409)
        if type(rating) is not int or rating not in {-1, 1} or category not in CATEGORIES:
            raise AgentHubError("FEEDBACK_INVALID", "Invalid rating or category.", 422)
        if (
            not client_key.strip()
            or len(client_key) > 64
            or len(comment) > 4000
            or (corrected_answer is not None and len(corrected_answer) > 16000)
        ):
            raise AgentHubError(
                "FEEDBACK_INVALID", "Invalid feedback length or submission key.", 422
            )
        if turn_id is not None:
            turn = await session.scalar(
                select(ThreadTurn).where(
                    ThreadTurn.workspace_id == run.workspace_id,
                    ThreadTurn.id == turn_id,
                    ThreadTurn.agent_run_id == run.id,
                    ThreadTurn.thread_id == run.thread_id,
                )
            )
            if turn is None:
                raise AgentHubError(
                    "FEEDBACK_TURN_MISMATCH", "The turn does not belong to this run.", 422
                )
        answer = corrected_answer.strip() if corrected_answer and corrected_answer.strip() else None
        content = dict(
            run_id=str(run.id),
            turn_id=str(turn_id) if turn_id else None,
            rating=rating,
            category=category,
            comment=comment.strip(),
            corrected_answer=answer,
        )
        request_hash = canonical_json_hash(content)
        query = select(RunFeedback).where(
            RunFeedback.workspace_id == run.workspace_id,
            RunFeedback.created_by == UUID(context.user_id),
            RunFeedback.client_key == client_key,
        )
        existing = await session.scalar(query)
        if existing is not None:
            return self._same_submission(existing, request_hash)
        feedback = RunFeedback(
            workspace_id=run.workspace_id,
            run_id=run.id,
            turn_id=turn_id,
            agent_version_id=run.agent_version_id,
            created_by=UUID(context.user_id),
            client_key=client_key,
            request_hash=request_hash,
            rating=rating,
            category=category,
            comment=comment.strip(),
            corrected_answer=answer,
        )
        session.add(feedback)
        try:
            await session.flush()
            audit(session, context, feedback, "feedback.submitted")
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(query)
            if existing is None:
                raise
            return self._same_submission(existing, request_hash)
        return feedback

    @staticmethod
    def _same_submission(feedback, request_hash):
        if feedback.request_hash != request_hash:
            raise AgentHubError(
                "FEEDBACK_IDEMPOTENCY_CONFLICT",
                "The submission key was already used for different content.",
                409,
            )
        return feedback

    async def list_for_run(self, session, context, run_id):
        run = await self._run(session, context, run_id)
        return list(
            await session.scalars(
                select(RunFeedback)
                .where(RunFeedback.workspace_id == run.workspace_id, RunFeedback.run_id == run.id)
                .order_by(RunFeedback.created_at.desc(), RunFeedback.id)
                .limit(100)
            )
        )

    async def _locked(self, session, context, feedback_id):
        workspace = require(context, "workspace_read", "evaluation_manage")
        row = await session.scalar(
            select(RunFeedback)
            .where(RunFeedback.workspace_id == workspace, RunFeedback.id == feedback_id)
            .with_for_update()
        )
        if row is None:
            raise AgentHubError("FEEDBACK_NOT_FOUND", "Feedback was not found.", 404)
        return row

    async def review(
        self, session, context, feedback_id, *, expected_version, decision, comment=""
    ):
        row = await self._locked(session, context, feedback_id)
        if decision not in {"APPROVED", "REJECTED"} or len(comment) > 4000:
            raise AgentHubError("FEEDBACK_INVALID", "Invalid review.", 422)
        if row.review_version != expected_version or row.status != "PENDING":
            if (
                row.status == decision
                and row.reviewed_by == UUID(context.user_id)
                and row.review_version == expected_version + 1
                and row.review_comment == comment.strip()
            ):
                return row
            raise AgentHubError(
                "FEEDBACK_REVIEW_CONFLICT", "Feedback has already been reviewed; reload it.", 409
            )
        row.status = decision
        row.review_version += 1
        row.reviewed_by, row.reviewed_at = UUID(context.user_id), datetime.now(UTC)
        row.review_comment = comment.strip()
        audit(session, context, row, "feedback.reviewed")
        await session.commit()
        return row

    async def import_dev(
        self, session, context, feedback_id, *, dataset_id, base_version_id, expected_review_version
    ):
        row = await self._locked(session, context, feedback_id)
        request_hash = canonical_json_hash(
            dict(
                dataset_id=str(dataset_id),
                base_version_id=str(base_version_id),
                expected_review_version=expected_review_version,
                content_revision=row.content_revision,
            )
        )
        if row.imported_version_id:
            if row.import_request_hash != request_hash:
                raise AgentHubError(
                    "FEEDBACK_IMPORT_CONFLICT",
                    "Feedback was already imported with another target.",
                    409,
                )
            return await session.get(EvaluationDatasetVersion, row.imported_version_id)
        if row.status != "APPROVED" or row.review_version != expected_review_version:
            raise AgentHubError(
                "FEEDBACK_REVIEW_REQUIRED", "Import requires the current approved review.", 409
            )
        if not row.corrected_answer:
            raise AgentHubError(
                "FEEDBACK_CORRECTION_REQUIRED", "A human correction is required.", 422
            )
        base = await session.scalar(
            select(EvaluationDatasetVersion).where(
                EvaluationDatasetVersion.workspace_id == row.workspace_id,
                EvaluationDatasetVersion.dataset_id == dataset_id,
                EvaluationDatasetVersion.id == base_version_id,
            )
        )
        if base is None:
            raise AgentHubError(
                "EVALUATION_DATASET_NOT_FOUND", "Dataset version was not found.", 404
            )
        splits = set(
            await session.scalars(
                select(EvaluationDatasetItem.split).where(
                    EvaluationDatasetItem.workspace_id == row.workspace_id,
                    EvaluationDatasetItem.dataset_version_id == base_version_id,
                )
            )
        )
        if splits != {"DEV"}:
            raise AgentHubError(
                "FEEDBACK_DEV_ONLY", "Feedback imports require a DEV-only base version.", 422
            )
        version = await EvaluationDatasetService().create_version_from_run(
            session,
            context=context,
            dataset_id=dataset_id,
            base_version_id=base_version_id,
            run_id=row.run_id,
            case_key=f"feedback-{row.id}",
            split="DEV",
            category="KNOWLEDGE_QA",
            expected={"answer": row.corrected_answer, "citations": []},
            tags=["human-reviewed"],
            provenance_extra={
                "feedback_id": str(row.id),
                "feedback_revision": row.content_revision,
                "reviewed_by": str(row.reviewed_by),
                "review_version": row.review_version,
            },
            commit=False,
        )
        row.imported_version_id, row.import_request_hash = version.id, request_hash
        audit(session, context, row, "feedback.imported_dev")
        await session.commit()
        return version
