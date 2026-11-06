"""Turn one finished thread turn into long-term memory, off the hot path.

This task keeps extraction off the answer's latency path; its model calls can
incur fees. The run has already
committed its status and the answer is already on its way back; everything here
happens afterwards, in a different process, and is allowed to fail silently. A
turn that succeeded and then produced no memory is a turn that succeeded.

Three checks are re-done here rather than trusted from the message, for the
same reason ``execute_agent_run`` re-derives permissions: a queue message is an
identifier, not an authorization.

* The run must still be SUCCEEDED and still belong to a thread.
* The *published version* must have ``memory.long_term_memory`` on. The API
  enqueues without reading the spec, so this is the only place the switch is
  actually enforced -- a replayed or forged message cannot make an agent learn
  something it was not configured to learn.
* Everything the model proposes goes through the write gate in
  ``packages.memory.store``, which is what decides length, kind and duplicates.

Extraction uses the agent's own published model plan. Adding a separate model
setting would be one more thing to configure, one more credential to resolve,
and one more way for an agent's memory to be written by a model its owner never
approved.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from apps.worker.celery_app import celery_app
from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
    ContextMessage,
)
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.control_plane.services import TenantService
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import PrincipalContext
from packages.memory.extraction import (
    TurnForExtraction,
    extraction_request,
    parse_candidates,
)
from packages.memory.models import MemoryExtractionJob
from packages.memory.queue import MEMORY_EXTRACTION_TASK_NAME, CeleryMemoryWriteQueue
from packages.memory.store import SqlAlchemyMemoryStore
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.observability import ProductionTraceSink

logger = logging.getLogger(__name__)
AGENT_RUN_PERMISSION = "agent_run"
MEMORY_RECONCILE_STALE_AFTER = timedelta(minutes=5)


class _ExtractionPhase(Enum):
    PREPARING = auto()
    MODEL_CALL_POSSIBLE = auto()
    RESPONSE_RECEIVED = auto()
    CANDIDATES_READY = auto()


def _owner_lock_key(workspace_id: UUID, run_id: UUID) -> str:
    return f"memory-extraction:{workspace_id}:{run_id}"


async def _try_own_extraction(session, workspace_id: UUID, run_id: UUID) -> bool:
    # The transaction remains open through extraction/recording. A live worker
    # cannot be mistaken for a crashed one; process loss releases this lock.
    return bool(
        await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": _owner_lock_key(workspace_id, run_id)},
        )
    )


async def _authorized_memory_context(session, *, principal, workspace_id):
    try:
        context = (
            await TenantService().get_workspace_access(
                session, principal=principal, workspace_id=workspace_id
            )
        ).context
    except AgentHubError as error:
        if error.status_code not in {403, 404}:
            raise
        return None
    return context if AGENT_RUN_PERMISSION in context.permissions else None


async def _claim_extraction(
    factory, *, workspace_id: UUID, run_id: UUID
) -> MemoryExtractionJob | None:
    """Create or resume a job without ever repeating an uncertain send.

    A RUNNING row means a worker may already have sent the request. Re-claiming
    it is allowed only to finish deterministic recording from a persisted
    candidate payload. After ownership has been acquired, a RUNNING row without
    that payload becomes a terminal unknown extraction, without changing the
    already-completed parent Run or risking a second provider call.
    """

    async with factory() as session:
        job = await session.scalar(
            select(MemoryExtractionJob)
            .where(
                MemoryExtractionJob.workspace_id == workspace_id,
                MemoryExtractionJob.agent_run_id == run_id,
            )
            .with_for_update()
        )
        if job is None:
            # Creation and claim share one INSERT/commit. A two-step PENDING ->
            # RUNNING transition lets a concurrent loser claim the row between
            # those commits and send the same private model request twice.
            job = MemoryExtractionJob(
                workspace_id=workspace_id,
                agent_run_id=run_id,
                status="RUNNING",
                claimed_at=datetime.now(UTC),
            )
            session.add(job)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            return job
        if job.status in {"COMPLETED", "SKIPPED"}:
            return None
        if job.status == "RUNNING":
            if job.candidate_payload is None:
                job.status = "FAILED"
                job.failure_code = "MEMORY_EXTRACTION_OUTCOME_UNKNOWN"
                job.completed_at = datetime.now(UTC)
                job.claimed_at = None
                await session.commit()
                return None
            # Keep the original RUNNING ownership marker. The write path below
            # re-checks the agent_run permission immediately before acting.
            return job
        # PENDING has never sent. FAILED is a terminal failure code and does
        # not prove that no request reached the provider, so it is never
        # replayed automatically. Persisting candidates precedes the
        # deterministic memory write, so a retry after a later write failure is
        # handled through the RUNNING branch.
        if job.status == "FAILED":
            return None
        job.status = "RUNNING"
        job.claimed_at = datetime.now(UTC)
        job.failure_code = None
        await session.commit()
        return job


async def _finish_extraction(
    factory,
    *,
    workspace_id: UUID,
    run_id: UUID,
    status: str,
    failure_code: str | None = None,
) -> None:
    async with factory() as session:
        values = {
            "status": status,
            "completed_at": None if status == "RUNNING" else datetime.now(UTC),
            "claimed_at": None,
            "failure_code": failure_code,
        }
        await session.execute(
            update(MemoryExtractionJob)
            .where(
                MemoryExtractionJob.workspace_id == workspace_id,
                MemoryExtractionJob.agent_run_id == run_id,
                MemoryExtractionJob.status == "RUNNING",
            )
            .values(**values)
        )
        await session.commit()


@celery_app.task(
    bind=True,
    name=MEMORY_EXTRACTION_TASK_NAME,
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def extract_run_memories(_task, workspace_id: str, run_id: str, request_id: str) -> None:
    try:
        parsed_workspace_id = UUID(workspace_id)
        parsed_run_id = UUID(run_id)
    except ValueError:
        return
    asyncio.run(
        _extract_run_memories(
            get_settings(),
            workspace_id=parsed_workspace_id,
            run_id=parsed_run_id,
            request_id=request_id,
        )
    )


async def _extract_run_memories(
    settings: Settings,
    *,
    workspace_id: UUID,
    run_id: UUID,
    request_id: str,
) -> None:
    engine, factory = create_database(settings.database_url)
    try:
        async with factory() as owner:
            async with owner.begin():
                if not await _try_own_extraction(owner, workspace_id, run_id):
                    return
                await _extract_owned_memories(
                    settings,
                    factory,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    request_id=request_id,
                )
    finally:
        await engine.dispose()


async def _extract_owned_memories(
    settings,
    factory,
    *,
    workspace_id: UUID,
    run_id: UUID,
    request_id: str,
) -> None:
    phase = _ExtractionPhase.PREPARING
    try:
        job = await _claim_extraction(factory, workspace_id=workspace_id, run_id=run_id)
        if job is None:
            return
        async with factory() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.workspace_id == workspace_id, AgentRun.id == run_id)
            )
            if run is None or run.status != "SUCCEEDED" or run.thread_id is None:
                await _finish_extraction(
                    factory, workspace_id=workspace_id, run_id=run_id, status="SKIPPED"
                )
                return
            if not run.input_text:
                await _finish_extraction(
                    factory, workspace_id=workspace_id, run_id=run_id, status="SKIPPED"
                )
                return
            version = await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.workspace_id == workspace_id,
                    AgentVersion.id == run.agent_version_id,
                )
            )
            if version is None:
                await _finish_extraction(
                    factory, workspace_id=workspace_id, run_id=run_id, status="SKIPPED"
                )
                return
            spec = parse_frozen_agent_spec(version.resolved_spec, workspace_id=workspace_id)
            if not spec.runtime.get("memory", {}).get("long_term_memory", False):
                await _finish_extraction(
                    factory, workspace_id=workspace_id, run_id=run_id, status="SKIPPED"
                )
                return
            turn = TurnForExtraction(user_input=run.input_text)
            agent_id = version.agent_id
            thread_id = run.thread_id
            principal = PrincipalContext(
                request_id=request_id,
                trace_id=request_id,
                user_id=str(run.created_by),
            )
            context = await _authorized_memory_context(
                session, principal=principal, workspace_id=workspace_id
            )
            if context is None:
                await _finish_extraction(
                    factory,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    status="SKIPPED",
                    failure_code="AGENT_RUN_FORBIDDEN",
                )
                return
            # Re-authorize immediately before the provider call. The earlier
            # membership read closes the dispatch race; this read closes a
            # role downgrade between preparation and model use.
            # Detach the values needed after the session closes; no ORM object
            # is used across the provider call or extraction write.
            agent_id = version.agent_id
            thread_id = run.thread_id
            input_text = run.input_text
            model_plan = spec.model_plan

        async with factory() as session:
            persisted_job = await session.scalar(
                select(MemoryExtractionJob).where(
                    MemoryExtractionJob.workspace_id == workspace_id,
                    MemoryExtractionJob.agent_run_id == run_id,
                )
            )
            if persisted_job is None or persisted_job.status != "RUNNING":
                return
            stored_payload = persisted_job.candidate_payload

        if stored_payload is None:
            request = extraction_request(turn)
            admitted = ContextBudgetPolicy(
                spec, ContextBudgetConfig(**spec.runtime["context_budget"])
            ).admit(
                tuple(
                    ContextMessage(
                        message,
                        ContextCategory.SYSTEM_PROMPT
                        if message.role == "system"
                        else ContextCategory.CURRENT_USER_TASK,
                    )
                    for message in request.messages
                )
            )
            request = replace(request, messages=admitted.messages)
            # Re-read the caller's current access immediately before the model
            # send. A workspace role can be downgraded while the job is queued,
            # and a persisted payload must not be treated as authorization to
            # perform another provider call.
            async with factory() as authorization_session:
                current_context = await _authorized_memory_context(
                    authorization_session, principal=principal, workspace_id=workspace_id
                )
            if current_context is None:
                await _finish_extraction(
                    factory,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    status="SKIPPED",
                    failure_code="AGENT_RUN_FORBIDDEN",
                )
                return
            trace_sink = ProductionTraceSink()
            async with factory() as session:
                gateway = SqlAlchemyModelGateway(
                    session,
                    credential_cipher=ProviderCredentialCipher.from_settings(settings),
                    trace_sink=trace_sink,
                )
                prepared = await gateway.prepare_resolved(
                    current_context, model_plan, request, operation="generate"
                )
            # Entering the detached gateway does not prove that bytes were
            # sent: it may still refuse locally. Without a returned response,
            # however, a send cannot safely be ruled out (including fallback).
            phase = _ExtractionPhase.MODEL_CALL_POSSIBLE
            response = await prepared.generate_resolved(current_context, model_plan, request)
            phase = _ExtractionPhase.RESPONSE_RECEIVED
            candidates = parse_candidates(response, user_input=input_text)
            payload = [asdict(candidate) for candidate in candidates]
            phase = _ExtractionPhase.CANDIDATES_READY
            async with factory() as session:
                persisted = await session.execute(
                    update(MemoryExtractionJob)
                    .where(
                        MemoryExtractionJob.workspace_id == workspace_id,
                        MemoryExtractionJob.agent_run_id == run_id,
                        MemoryExtractionJob.status == "RUNNING",
                        MemoryExtractionJob.candidate_payload.is_(None),
                    )
                    .values(candidate_payload=payload)
                )
                await session.commit()
                if persisted.rowcount != 1:
                    # A lost ownership connection may have been reconciled to
                    # UNKNOWN while the original HTTP request was still in flight.
                    # Its late response must not revive the terminal job or write.
                    return
        else:
            from packages.memory.contracts import MemoryCandidate

            candidates = tuple(MemoryCandidate(**item) for item in stored_payload)
        if not candidates:
            await _finish_extraction(
                factory, workspace_id=workspace_id, run_id=run_id, status="COMPLETED"
            )
            return
        # Persisted candidates are evidence of a response, never authorization
        # to write after the actor's membership was revoked during extraction.
        async with factory() as authorization_session:
            write_context = await _authorized_memory_context(
                authorization_session, principal=principal, workspace_id=workspace_id
            )
        if write_context is None:
            await _finish_extraction(
                factory,
                workspace_id=workspace_id,
                run_id=run_id,
                status="SKIPPED",
                failure_code="AGENT_RUN_FORBIDDEN",
            )
            return
        created = await SqlAlchemyMemoryStore(factory).record(
            workspace_id=workspace_id,
            agent_id=agent_id,
            thread_id=thread_id,
            source_run_id=run_id,
            candidates=candidates,
        )
        logger.info(
            "memory_extraction_completed",
            extra={"proposed": len(candidates), "memories_created": len(created)},
        )
        await _finish_extraction(
            factory, workspace_id=workspace_id, run_id=run_id, status="COMPLETED"
        )
    except Exception:
        # Preserve the successful parent Run. Only persisted candidates may
        # resume deterministic recording; other failures are terminal and
        # must never be repaired by another model call. Local response stages
        # distinguish known-response processing/storage failure from a call
        # whose outcome is unknown. Process loss still reconciles conservatively.
        try:
            async with factory() as failure_session:
                persisted = await failure_session.get(
                    MemoryExtractionJob,
                    {"workspace_id": workspace_id, "agent_run_id": run_id},
                )
                if persisted is not None and persisted.candidate_payload is not None:
                    final_status = "RUNNING"
                    failure_code = "MEMORY_EXTRACTION_RECORDING_PENDING"
                else:
                    final_status = "FAILED"
                    failure_code = {
                        _ExtractionPhase.PREPARING: "MEMORY_EXTRACTION_FAILED",
                        _ExtractionPhase.MODEL_CALL_POSSIBLE: "MEMORY_EXTRACTION_OUTCOME_UNKNOWN",
                        _ExtractionPhase.RESPONSE_RECEIVED: (
                            "MEMORY_EXTRACTION_RESPONSE_PROCESSING_FAILED"
                        ),
                        _ExtractionPhase.CANDIDATES_READY: (
                            "MEMORY_EXTRACTION_RESPONSE_PERSISTENCE_FAILED"
                        ),
                    }[phase]
            await _finish_extraction(
                factory,
                workspace_id=workspace_id,
                run_id=run_id,
                status=final_status,
                failure_code=failure_code,
            )
        except Exception:
            logger.warning("memory_extraction_failure_bookkeeping_failed")
        logger.warning("memory_extraction_failed")


async def _reconcile_memory_jobs(factory, *, enqueue, now=None, limit=50, workspace_id=None):
    """Recover unsent intents or persisted candidates, never uncertain sends.

    Queue delivery is at least once. Updated timestamps bound repeat enqueues;
    extraction ownership and source-run contribution keys make delivery safe.
    """
    now = now or datetime.now(UTC)
    queued = []
    counts = {"unknown": 0, "enqueued": 0, "active": 0, "enqueue_failed": 0}
    async with factory() as session:
        statement = select(MemoryExtractionJob)
        if workspace_id is not None:
            statement = statement.where(MemoryExtractionJob.workspace_id == workspace_id)
        jobs = list(
            await session.scalars(
                statement.where(
                    MemoryExtractionJob.status.in_(("PENDING", "RUNNING")),
                    MemoryExtractionJob.updated_at <= now - MEMORY_RECONCILE_STALE_AFTER,
                )
                .order_by(MemoryExtractionJob.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for job in jobs:
            if not await _try_own_extraction(session, job.workspace_id, job.agent_run_id):
                counts["active"] += 1
                continue
            if job.status == "RUNNING" and job.candidate_payload is None:
                job.status = "FAILED"
                job.failure_code = "MEMORY_EXTRACTION_OUTCOME_UNKNOWN"
                job.claimed_at = None
                job.completed_at = now
                counts["unknown"] += 1
            else:
                job.claimed_at = now
                job.completed_at = None
                job.updated_at = now
                job.failure_code = (
                    "MEMORY_EXTRACTION_DISPATCH_PENDING"
                    if job.status == "PENDING"
                    else "MEMORY_EXTRACTION_RECORDING_PENDING"
                )
                queued.append((job.workspace_id, job.agent_run_id))
        await session.commit()
    for workspace_id, run_id in queued:
        try:
            await enqueue(
                workspace_id=workspace_id, run_id=run_id, request_id=f"memory-reconcile-{run_id}"
            )
            counts["enqueued"] += 1
        except Exception:
            counts["enqueue_failed"] += 1
            logger.warning("memory_extraction_reconcile_enqueue_failed")
    return counts


async def _reconcile_memories(settings):
    engine, factory = create_database(settings.database_url)
    try:
        return await _reconcile_memory_jobs(
            factory, enqueue=CeleryMemoryWriteQueue(celery_app).enqueue
        )
    finally:
        await engine.dispose()


@celery_app.task(name="agenthub.reconcile_memories", ignore_result=True)
def reconcile_memories():
    return asyncio.run(_reconcile_memories(get_settings()))


__all__ = ["extract_run_memories", "reconcile_memories"]
