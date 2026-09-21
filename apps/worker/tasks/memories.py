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
from uuid import UUID

from sqlalchemy import select

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
from packages.memory.queue import MEMORY_EXTRACTION_TASK_NAME
from packages.memory.store import SqlAlchemyMemoryStore
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.observability import ProductionTraceSink

logger = logging.getLogger(__name__)


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
        async with factory() as session:
            run = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == workspace_id, AgentRun.id == run_id
                )
            )
            if run is None or run.status != "SUCCEEDED" or run.thread_id is None:
                return
            if not run.input_text or not run.final_output:
                return
            version = await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.workspace_id == workspace_id,
                    AgentVersion.id == run.agent_version_id,
                )
            )
            if version is None:
                return
            spec = parse_frozen_agent_spec(version.resolved_spec, workspace_id=workspace_id)
            if not spec.runtime.get("memory", {}).get("long_term_memory", False):
                return
            turn = TurnForExtraction(
                user_input=run.input_text, final_output=run.final_output
            )
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

        trace_sink = ProductionTraceSink()
        async with factory() as session:
            gateway = SqlAlchemyModelGateway(
                session,
                credential_cipher=ProviderCredentialCipher.from_settings(settings),
                trace_sink=trace_sink,
            )
            prepared = await gateway.prepare_resolved(
                context, spec.model_plan, extraction_request(turn), operation="generate"
            )
        # Outside the session on purpose: provider latency must not hold a
        # database connection, exactly as in the run path.
        response = await prepared.generate_resolved(
            context, spec.model_plan, extraction_request(turn)
        )
        candidates = parse_candidates(response)
        if not candidates:
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
    except Exception:
        # A failed extraction must never look like a failed run, and there is
        # nothing a retry would fix that the next turn will not.
        logger.warning("memory_extraction_failed", exc_info=True)
    finally:
        await engine.dispose()


__all__ = ["extract_run_memories"]
