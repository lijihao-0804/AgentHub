"""Execute scripted turns with a pinned version through existing thread/runtime services."""

from __future__ import annotations

from collections.abc import Sequence
from time import perf_counter
from uuid import UUID, uuid4

from sqlalchemy import select

from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.agent_runtime.runtime import AgentRunExecutionOverrides, AgentRunService
from packages.approvals.contracts import ApprovalDecisionStatus
from packages.approvals.models import Approval
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.observations import observation_identity
from packages.observability.timing import capture_timings
from packages.threads.service import ThreadService


class ScriptedScenarioDriver:
    """One invocation creates one isolated conversation, without resolving LATEST.

    Business fixtures and labels belong to the caller; this driver records execution
    identity only. Callers must inspect persistent business facts separately. Failed or
    uncertain runs stop the scenario instead of silently continuing/retrying a write.
    """

    def __init__(self, runtime: AgentRunService, *, driver_kind: str):
        if driver_kind not in {"runtime", "controlled"}:
            raise ValueError("SCENARIO_DRIVER_IDENTITY_REQUIRED")
        self.runtime = runtime
        self.threads = ThreadService(runtime.session_factory, run_service=runtime)
        self.driver_kind = driver_kind

    async def execute(
        self,
        context: WorkspaceExecutionContext,
        *,
        case_id: str,
        agent_version_id: UUID,
        user_turns: Sequence[str],
        knowledge_snapshots: Sequence[dict[str, str]],
        approval_decisions: Sequence[str] = (),
        approval_context: WorkspaceExecutionContext | None = None,
    ) -> dict:
        if "evaluation_run" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "Evaluation permission is required.", 403)
        if (
            not case_id
            or not 1 <= len(user_turns) <= 10
            or any(not isinstance(t, str) or not t.strip() or len(t) > 32000 for t in user_turns)
            or len(approval_decisions) > 10
            or any(d not in {"APPROVED", "DENIED"} for d in approval_decisions)
        ):
            raise ValueError("INVALID_SCRIPTED_SCENARIO")
        approver = approval_context or context
        if approver.workspace_id != context.workspace_id:
            raise AgentHubError("FORBIDDEN", "Approval context belongs to another workspace.", 403)
        async with self.runtime.session_factory() as session:
            version = await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.workspace_id == UUID(context.workspace_id),
                    AgentVersion.id == agent_version_id,
                )
            )
            if version is None:
                raise AgentHubError("AGENT_VERSION_NOT_FOUND", "Agent version was not found.", 404)
            agent_id, spec_hash = version.agent_id, version.resolved_spec_hash
        instance_id = uuid4().hex
        thread = await self.threads.create_thread(
            context, agent_id=agent_id, title=f"evaluation-{instance_id}", kind="support"
        )
        started = perf_counter()
        runs, decision_index, reason = [], 0, None
        with capture_timings() as timing:
            for ordinal, text in enumerate(user_turns):
                turn = await self.threads.open_turn(
                    context, thread.id, user_input=text, client_token=f"{instance_id}:{ordinal}"
                )
                if turn is None:
                    raise RuntimeError("NEW_SCENARIO_TURN_CONFLICT")
                prepared = await self.runtime.prepare_run(
                    context,
                    agent_version_id=agent_version_id,
                    input_text=text,
                    thread_id=thread.id,
                    execution_overrides=AgentRunExecutionOverrides.for_evaluation(
                        list(knowledge_snapshots)
                    ),
                )
                # Persist the turn/run relation before any model or tool call.
                await self.threads.attach_run(context, turn_id=turn.id, run_id=prepared.id)
                result = await self.runtime.execute_prepared_run(context, run_id=prepared.id)
                while result.status == "WAITING_APPROVAL":
                    if decision_index >= len(approval_decisions):
                        reason = "approval_script_exhausted"
                        break
                    async with self.runtime.session_factory() as session:
                        approval = await session.scalar(
                            select(Approval)
                            .where(
                                Approval.workspace_id == UUID(context.workspace_id),
                                Approval.run_id == result.run_id,
                                Approval.decision_status == "PENDING",
                            )
                            .order_by(Approval.created_at, Approval.id)
                        )
                    if approval is None or self.runtime.approval_service is None:
                        raise RuntimeError("SCENARIO_PENDING_APPROVAL_UNAVAILABLE")
                    await self.runtime.approval_service.decide(
                        approver,
                        approval.id,
                        decision=ApprovalDecisionStatus(approval_decisions[decision_index]),
                    )
                    decision_index += 1
                    result = await self.runtime.resume(
                        context, run_id=result.run_id, approval_id=approval.id
                    )
                async with self.runtime.session_factory() as session:
                    persisted = await session.get(AgentRun, result.run_id)
                    if (
                        persisted is None
                        or persisted.resolved_spec_hash != spec_hash
                        or persisted.agent_version_id != agent_version_id
                        or persisted.effective_knowledge_snapshots != list(knowledge_snapshots)
                    ):
                        raise RuntimeError("SCENARIO_FROZEN_IDENTITY_MISMATCH")
                    runs.append(
                        {
                            "run_id": str(result.run_id),
                            "turn_id": str(turn.id),
                            "status": persisted.status,
                            "failure_code": persisted.failure_code,
                            "resolved_spec_hash": persisted.resolved_spec_hash,
                            "output_hash": canonical_json_hash(persisted.final_output or ""),
                            "input_tokens": persisted.total_input_tokens,
                            "output_tokens": persisted.total_output_tokens,
                            "cost_amount": str(persisted.total_cost_amount)
                            if persisted.total_cost_amount is not None
                            else None,
                            "cost_currency": persisted.cost_currency,
                        }
                    )
                if result.status != "SUCCEEDED":
                    reason = reason or f"run_{result.status.lower()}"
                    break
        return {
            **observation_identity(self.driver_kind, {"turns": "persisted_thread_agent_runs"}),
            "scenario_driver_version": "scripted-scenario-v1",
            "case_id": case_id,
            "instance_id": instance_id,
            "thread_id": str(thread.id),
            "agent_version_id": str(agent_version_id),
            "resolved_spec_hash": spec_hash,
            "knowledge_snapshots_hash": canonical_json_hash(list(knowledge_snapshots)),
            "planned_turn_count": len(user_turns),
            "executed_turn_count": len(runs),
            "turns": runs,
            "approval_decision_count": decision_index,
            "unused_approval_decision_count": len(approval_decisions) - decision_index,
            "execution_complete": len(runs) == len(user_turns) and reason is None,
            "stop_reason": reason,
            "latency_ms": (perf_counter() - started) * 1000,
            "timing": timing.projection(),
        }
