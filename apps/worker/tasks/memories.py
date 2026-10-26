"""Turn one finished thread turn into long-term memory, off the hot path.

This task exists so that learning costs the user nothing. The run has already
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
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from apps.worker.celery_app import celery_app
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.control_plane.services import TenantService
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.memory.extraction import (
    TurnForExtraction,
    extraction_request,
    parse_candidates,
)
from packages.memory.models import MemoryExtractionJob
from packages.memory.queue import MEMORY_EXTRACTION_TASK_NAME
from packages.memory.store import SqlAlchemyMemoryStore
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.observability import ProductionTraceSink

logger = logging.getLogger(__name__)
AGENT_RUN_PERMISSION = "agent_run"


async def _claim_extraction(
    factory, *, workspace_id: UUID, run_id: UUID
) -> MemoryExtractionJob | None:
    """Create or resume a job without ever repeating an uncertain send.

    A RUNNING row means a worker may already have sent the request. Re-claiming
    it is allowed only to finish deterministic recording from a persisted
    candidate payload; a RUNNING row without that payload must be fenced to
    NEEDS_ATTENTION rather than risk a second provider call.
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
            "completed_at": datetime.now(UTC),
            "claimed_at": None,
        }
        if failure_code is not None:
            values["failure_code"] = failure_code
        await session.execute(
            update(MemoryExtractionJob)
            .where(
                MemoryExtractionJob.workspace_id == workspace_id,
                MemoryExtractionJob.agent_run_id == run_id,
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
            context = (
                await TenantService().get_workspace_access(
                    session, principal=principal, workspace_id=workspace_id
                )
            ).context
            if AGENT_RUN_PERMISSION not in context.permissions:
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
            # Re-read the caller's current access immediately before the model
            # send. A workspace role can be downgraded while the job is queued,
            # and a persisted payload must not be treated as authorization to
            # perform another provider call.
            async with factory() as authorization_session:
                current_context = (
                    await TenantService().get_workspace_access(
                        authorization_session,
                        principal=principal,
                        workspace_id=workspace_id,
                    )
                ).context
            if AGENT_RUN_PERMISSION not in current_context.permissions:
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
                    context, model_plan, extraction_request(turn), operation="generate"
                )
            response = await prepared.generate_resolved(
                context, model_plan, extraction_request(turn)
            )
            candidates = parse_candidates(response, user_input=input_text)
            payload = [asdict(candidate) for candidate in candidates]
            async with factory() as session:
                await session.execute(
                    update(MemoryExtractionJob)
                    .where(
                        MemoryExtractionJob.workspace_id == workspace_id,
                        MemoryExtractionJob.agent_run_id == run_id,
                        MemoryExtractionJob.status == "RUNNING",
                    )
                    .values(candidate_payload=payload)
                )
                await session.commit()
        else:
            from packages.memory.contracts import MemoryCandidate

            candidates = tuple(MemoryCandidate(**item) for item in stored_payload)
        if not candidates:
            await _finish_extraction(
                factory, workspace_id=workspace_id, run_id=run_id, status="COMPLETED"
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
            extra={"proposed": len(candidates), "created": len(created)},
        )
        await _finish_extraction(
            factory, workspace_id=workspace_id, run_id=run_id, status="COMPLETED"
        )
    except Exception:
        # A failed extraction must never look like a failed run. Candidates
        # persisted before deterministic recording can be resumed without a
        # second model send; before that boundary the job is terminal, because
        # a request may already have reached the provider.
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
                    failure_code = "MEMORY_EXTRACTION_FAILED"
            await _finish_extraction(
                factory,
                workspace_id=workspace_id,
                run_id=run_id,
                status=final_status,
                failure_code=failure_code,
            )
        except Exception:
            logger.warning("memory_extraction_failure_bookkeeping_failed", exc_info=True)
        logger.warning("memory_extraction_failed", exc_info=True)
    finally:
        await engine.dispose()


__all__ = ["extract_run_memories"]
