"""Dynamic multi-turn interactive evaluation driver (AE-6b).

The driver owns no model or scoring logic.  It creates an isolated environment,
asks the simulator for the opening, executes real AgentRun turns through
ThreadService/AgentRunService, feeds the agent's actual output to the simulator for
the next user turn, then asserts the environment's business state and tears it down.

The simulator only sees the user-visible task and its own disclosure policy.  The
``expected`` block is used only after execution by ``environment.assert_state`` and
the evaluator; it is never passed to the agent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from sqlalchemy import select

from packages.agent_runtime.models import AgentRun, RunStep
from packages.agent_runtime.runtime import AgentRunExecutionOverrides
from packages.approvals.contracts import ApprovalDecisionStatus
from packages.approvals.models import Approval
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.environment import StateAssertion, TaskEnvironment
from packages.evaluation.models import EvaluationDatasetItem, EvaluationExperimentRun
from packages.evaluation.runner import CaseExecutionObservation, PreparedCaseExecution
from packages.evaluation.scenario_driver import ScriptedScenarioDriver
from packages.evaluation.trajectory import build_trajectory_observation
from packages.evaluation.user_simulator import ScriptedUserSimulator, Turn


@dataclass(frozen=True)
class InteractiveSimulatorSpec:
    opening_text: str
    private_facts: Mapping[str, str]
    triggers: Mapping[str, Sequence[str]]
    fallback: str
    max_turns: int


def simulator_from_item(
    item: EvaluationDatasetItem, *, customer_ref: str | None = None
) -> ScriptedUserSimulator:
    raw = item.input.get("simulator")
    if not isinstance(raw, Mapping):
        raise ValueError("INTERACTIVE_SIMULATOR_CONFIG_REQUIRED")
    try:
        private_facts = {str(k): str(v) for k, v in dict(raw["private_facts"]).items()}
        if customer_ref is not None:
            private_facts = {
                key: value.replace("__CUSTOMER_REF__", customer_ref)
                for key, value in private_facts.items()
            }
        spec = InteractiveSimulatorSpec(
            opening_text=str(item.input["task"]),
            private_facts=private_facts,
            triggers={
                str(k): [str(v) for v in values]
                for k, values in dict(raw["triggers"]).items()
            },
            fallback=str(raw.get("fallback", "我不太明白，你能直接帮我处理吗？")),
            max_turns=int(raw.get("max_turns", 4)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("INTERACTIVE_SIMULATOR_CONFIG_INVALID") from error
    return ScriptedUserSimulator(
        opening_text=spec.opening_text,
        private_facts=spec.private_facts,
        triggers=spec.triggers,
        fallback=spec.fallback,
        max_turns=spec.max_turns,
    )


class InteractiveScenarioDriver:
    kind = "interactive_runtime"
    version = "interactive-driver-v1"

    def __init__(
        self,
        scenarios: ScriptedScenarioDriver,
        *,
        context_factory: Callable[
            [EvaluationExperimentRun], Awaitable[WorkspaceExecutionContext]
        ],
        environment_factory: Callable[
            [WorkspaceExecutionContext, EvaluationDatasetItem], TaskEnvironment
        ],
        approval_context_factory: Callable[
            [EvaluationExperimentRun], Awaitable[WorkspaceExecutionContext]
        ]
        | None = None,
    ) -> None:
        self.scenarios = scenarios
        self.context_factory = context_factory
        self.environment_factory = environment_factory
        self.approval_context_factory = approval_context_factory

    async def prepare(self, *, run, variant, item) -> PreparedCaseExecution:
        if item.category != "INTERACTIVE":
            raise ValueError("INTERACTIVE_CATEGORY_REQUIRED")
        context = await self.context_factory(run)
        if str(context.workspace_id) != str(run.workspace_id):
            raise AgentHubError(
                "FORBIDDEN", "The interactive case belongs to another workspace.", 403
            )
        if "evaluation_run" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "Evaluation permission is required.", 403)
        assertions = item.expected.get("assertions")
        if not isinstance(assertions, Mapping):
            raise ValueError("INTERACTIVE_ASSERTIONS_REQUIRED")
        canary = str(item.expected.get("canary") or "")
        environment = self.environment_factory(context, item)
        handle = await environment.reset(
            seed=int(item.input.get("seed", 0)),
            fixture_ref=str(item.input.get("environment_ref", "support_customer")),
        )
        try:
            simulator = simulator_from_item(
                item,
                customer_ref=(
                    environment.customer_ref(handle)
                    if hasattr(environment, "customer_ref")
                    else None
                ),
            )
            opening = await simulator.opening()
            prepared = await self.scenarios.prepare_first(
                context,
                agent_version_id=variant.agent_version_id,
                first_input=opening,
                knowledge_snapshots=variant.effective_knowledge_snapshots,
            )
            max_turns = int(
                item.input.get(
                    "max_turns", item.input.get("simulator", {}).get("max_turns", 4)
                )
            )
            max_tokens = item.input.get("max_tokens")
            max_cost = item.input.get("max_cost_cny")
            approval_decisions = list(item.expected.get("approval_decisions") or [])
            approver = (
                await self.approval_context_factory(run)
                if approval_decisions and self.approval_context_factory is not None
                else context
            )
            if approver.workspace_id != context.workspace_id:
                raise AgentHubError(
                    "FORBIDDEN", "Approval context belongs to another workspace.", 403
                )
        except Exception:
            # Reset may have created the isolated customer; if preparation cannot finish,
            # remove that instance rather than leaving an unowned fixture behind.
            try:
                await environment.teardown(handle)
            except Exception:
                pass
            raise

        async def execute() -> CaseExecutionObservation:
            started = perf_counter()
            first_input = opening
            turns: list[dict] = []
            transcript: list[Turn] = []
            proposed_tool_sequence: list[str] = []
            executed_tool_sequence: list[str] = []
            execution_evidence: list[dict] = []
            decision_index = 0
            stop_reason: str | None = None
            assertion = StateAssertion(False, {}, "not_asserted")
            teardown_status = "NOT_STARTED"
            teardown_error = None
            total_input = 0
            total_output = 0
            total_cached = 0
            total_cost = 0.0
            currency: str | None = None
            known_cost = True
            known_tokens = True
            canary_leaked = False
            prepared_run = prepared
            user_text = first_input
            run_ids: list[UUID] = []
            try:
                for turn_index in range(max_turns):
                    result = await self.scenarios.runtime.execute_prepared_run(
                        context, run_id=prepared_run.run_id
                    )
                    while result.status == "WAITING_APPROVAL":
                        if decision_index >= len(approval_decisions):
                            stop_reason = "approval_script_exhausted"
                            break
                        async with self.scenarios.runtime.session_factory() as session:
                            approval = await session.scalar(
                                select(Approval)
                                .where(
                                    Approval.workspace_id == UUID(context.workspace_id),
                                    Approval.run_id == result.run_id,
                                    Approval.decision_status == "PENDING",
                                )
                                .order_by(Approval.created_at, Approval.id)
                            )
                        if approval is None or self.scenarios.runtime.approval_service is None:
                            stop_reason = "approval_unavailable"
                            break
                        await self.scenarios.runtime.approval_service.decide(
                            approver,
                            approval.id,
                            decision=ApprovalDecisionStatus(approval_decisions[decision_index]),
                        )
                        decision_index += 1
                        result = await self.scenarios.runtime.resume(
                            context, run_id=result.run_id, approval_id=approval.id
                        )
                    async with self.scenarios.runtime.session_factory() as session:
                        persisted = await session.get(AgentRun, result.run_id)
                        if persisted is None:
                            raise RuntimeError("INTERACTIVE_RUN_MISSING")
                        final_output = str(persisted.final_output or "")
                        input_tokens = persisted.total_input_tokens
                        output_tokens = persisted.total_output_tokens
                        cached_tokens = persisted.total_cached_tokens
                        cost_amount = persisted.total_cost_amount
                        if input_tokens is None or output_tokens is None:
                            known_tokens = False
                        else:
                            total_input += input_tokens
                            total_output += output_tokens
                            total_cached += cached_tokens or 0
                        if cost_amount is None:
                            known_cost = False
                        else:
                            total_cost += float(cost_amount)
                        if persisted.cost_currency:
                            currency = persisted.cost_currency
                        step_rows = list(
                            await session.scalars(
                                select(RunStep)
                                .where(
                                    RunStep.workspace_id == UUID(context.workspace_id),
                                    RunStep.agent_run_id == result.run_id,
                                    RunStep.kind.in_(
                                        (
                                            "TOOL_PROPOSAL",
                                            "TOOL_EXECUTE",
                                            "POLICY",
                                            "APPROVAL_WAIT",
                                            "ACTION",
                                            "GUARD",
                                            "FINISH",
                                        )
                                    ),
                                )
                                .order_by(RunStep.sequence_number, RunStep.id)
                            )
                        )
                        approval_rows = list(
                            await session.scalars(
                                select(Approval)
                                .where(
                                    Approval.workspace_id == UUID(context.workspace_id),
                                    Approval.run_id == result.run_id,
                                )
                                .order_by(Approval.created_at, Approval.id)
                            )
                        )
                        trajectory = build_trajectory_observation(
                            steps=[
                                {
                                    "index": index,
                                    "sequence_number": row.sequence_number,
                                    "kind": row.kind,
                                    "status": row.status,
                                    "error_code": (row.safe_metadata or {}).get("error_code"),
                                    "tool_identities": (row.safe_metadata or {}).get(
                                        "tool_identities"
                                    )
                                    or [],
                                    "arguments_hashes": (row.safe_metadata or {}).get(
                                        "arguments_hashes"
                                    )
                                    or [],
                                    "tool_results": (row.safe_metadata or {}).get("tool_results")
                                    or [],
                                    "logical_action_ids": (row.safe_metadata or {}).get(
                                        "logical_action_ids"
                                    )
                                    or [],
                                    "action_results": (row.safe_metadata or {}).get(
                                        "action_results"
                                    )
                                    or [],
                                }
                                for index, row in enumerate(step_rows)
                            ],
                            approvals=[
                                {
                                    "approval_id": str(row.id),
                                    "logical_action_id": row.logical_action_id,
                                    "tool_identity": row.tool_identity,
                                    "arguments_hash": row.canonical_args_hash,
                                    "decision_status": row.decision_status,
                                    "execution_status": row.execution_status,
                                    "failure_code": row.failure_code,
                                }
                                for row in approval_rows
                            ],
                            terminal={
                                "status": persisted.status,
                                "failure_code": persisted.failure_code,
                            },
                            counters={"tool_call_count": persisted.tool_call_count},
                        )
                        turn_proposed = trajectory.get("proposed_tool_sequence") or []
                        turn_executed = trajectory.get("executed_tool_sequence") or []
                        proposed_tool_sequence.extend(str(item) for item in turn_proposed)
                        executed_tool_sequence.extend(str(item) for item in turn_executed)
                        execution_evidence.extend(
                            item
                            for item in (trajectory.get("execution_evidence") or [])
                            if isinstance(item, Mapping)
                        )
                        run_ids.append(result.run_id)
                        turns.append(
                            {
                                "turn_index": turn_index,
                                "user_input_hash": canonical_json_hash(user_text),
                                "user_input_length": len(user_text),
                                "run_id": str(result.run_id),
                                "status": persisted.status,
                                "failure_code": persisted.failure_code,
                                "agent_output_hash": canonical_json_hash(final_output),
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "cost_amount": (
                                    str(cost_amount) if cost_amount is not None else None
                                ),
                                "proposed_tool_sequence": [
                                    str(item) for item in turn_proposed
                                ],
                                "executed_tool_sequence": [
                                    str(item) for item in turn_executed
                                ],
                            }
                        )
                    transcript.extend(
                        [
                            Turn("user", user_text),
                            Turn("assistant", final_output),
                        ]
                    )
                    if canary and canary in final_output:
                        canary_leaked = True
                        stop_reason = "hidden_label_leak"
                        break
                    if result.status in {"FAILED", "CANCELLED", "NEEDS_ATTENTION"}:
                        stop_reason = f"agent_{result.status.lower()}"
                        break
                    if result.status != "SUCCEEDED":
                        stop_reason = "agent_not_successful"
                        break
                    # Assert the world after each turn.  This is what makes a completed
                    # answer insufficient: only the environment can stop the dialogue.
                    turn_assertions = dict(assertions)
                    turn_assertions["turn_count"] = turn_index + 1
                    assertion = await environment.assert_state(handle, turn_assertions)
                    min_turns = int(assertions.get("min_turns", 1))
                    approvals_satisfied = decision_index >= len(approval_decisions)
                    if assertion.passed and turn_index + 1 >= min_turns and approvals_satisfied:
                        stop_reason = "goal_met"
                        break
                    if max_tokens is not None and known_tokens and (
                        total_input + total_output >= int(max_tokens)
                    ):
                        stop_reason = "budget_exhausted"
                        break
                    if max_cost is not None and known_cost and total_cost >= float(max_cost):
                        stop_reason = "budget_exhausted"
                        break
                    if simulator.exhausted() or turn_index + 1 >= max_turns:
                        stop_reason = "max_turns"
                        break
                    user_text = await simulator.respond(
                        transcript=transcript, agent_message=final_output
                    )
                    if not user_text or user_text.startswith("（对话已结束）"):
                        stop_reason = "simulator_exhausted"
                        break
                    next_turn = await self.scenarios.threads.open_turn(
                        context,
                        prepared.thread_id,
                        user_input=user_text,
                        client_token=f"{prepared.instance_id}:{turn_index + 1}",
                    )
                    next_run = await self.scenarios.runtime.prepare_run(
                        context,
                        agent_version_id=variant.agent_version_id,
                        input_text=user_text,
                        thread_id=prepared.thread_id,
                        thread_turn_id=next_turn.id,
                        execution_overrides=AgentRunExecutionOverrides.for_evaluation(
                            list(variant.effective_knowledge_snapshots)
                        ),
                    )
                    await self.scenarios.threads.attach_run(
                        context, turn_id=next_turn.id, run_id=next_run.id
                    )
                    prepared_run = type(prepared)(
                        prepared.instance_id,
                        prepared.thread_id,
                        next_turn.id,
                        next_run.id,
                        prepared.workspace_id,
                        prepared.spec_hash,
                    )
                if stop_reason is None:
                    stop_reason = "max_turns"
                final_assertions = dict(assertions)
                final_assertions["turn_count"] = len(turns)
                assertion = await environment.assert_state(handle, final_assertions)
            finally:
                try:
                    await environment.teardown(handle)
                    teardown_status = "TEARDOWN_COMPLETE"
                except Exception as error:  # noqa: BLE001 - surfaced as a failed trial, never swallowed
                    teardown_status = "TEARDOWN_FAILED"
                    teardown_error = type(error).__name__
            if teardown_status != "TEARDOWN_COMPLETE":
                stop_reason = "environment_teardown_failed"
            safe_assertion = {
                "passed": assertion.passed,
                "reason": assertion.reason,
                "observed": {
                    "ticket_count": assertion.observed.get("ticket_count"),
                    "ticket_statuses": assertion.observed.get("ticket_statuses", []),
                },
            }
            observation = {
                "category": "INTERACTIVE",
                "interactive_driver_version": self.version,
                "instance_id": prepared.instance_id,
                "thread_id": str(prepared.thread_id),
                "agent_version_id": str(variant.agent_version_id),
                "resolved_spec_hash": prepared.spec_hash,
                "planned_max_turns": max_turns,
                "executed_turn_count": len(turns),
                "turns": turns,
                "proposed_tool_sequence": proposed_tool_sequence,
                "executed_tool_sequence": executed_tool_sequence,
                "tool_sequence": executed_tool_sequence,
                "execution_evidence": execution_evidence,
                "stop_reason": stop_reason,
                "execution_complete": teardown_status == "TEARDOWN_COMPLETE",
                "agent_execution_complete": bool(turns)
                and all(str(turn["status"]).upper() == "SUCCEEDED" for turn in turns),
                "environment_lifecycle_status": teardown_status,
                "environment": {
                    "kind": environment.kind,
                    "version": environment.version,
                    "handle": handle.projection(),
                    "assertion": safe_assertion,
                    "teardown_status": teardown_status,
                    "teardown_error": teardown_error,
                },
                "simulator": {
                    "algorithm": "scripted-disclosure-v1",
                    "goal": dict(simulator.goal()),
                    "disclosure_policy_hash": canonical_json_hash(
                        dict(simulator.disclosure_policy())
                    ),
                    "max_turns": max_turns,
                },
                "disclosure_policy_respected": not canary_leaked,
                "canary_leaked": canary_leaked,
                "approval_decision_count": decision_index,
                "business_outcome": safe_assertion,
                "cost_amount": total_cost if known_cost else None,
                "cost_currency": currency if known_cost else None,
                "total_tokens": total_input + total_output if known_tokens else None,
            }
            return CaseExecutionObservation(
                observation=observation,
                agent_run_id=run_ids[0] if run_ids else prepared.run_id,
                latency_ms=round((perf_counter() - started) * 1000),
                input_tokens=total_input if known_tokens else None,
                output_tokens=total_output if known_tokens else None,
                cached_tokens=total_cached if known_tokens else None,
                total_tokens=total_input + total_output if known_tokens else None,
                cost_amount=total_cost if known_cost else None,
                cost_currency=currency if known_cost else None,
                aggregate_billing_complete=known_tokens and known_cost,
            )

        return PreparedCaseExecution(
            prepared.run_id,
            execute,
            preparation_observation={
                "interactive_preparation": {
                    "driver_version": self.version,
                    "instance_id": prepared.instance_id,
                    "thread_id": str(prepared.thread_id),
                    "environment_kind": environment.kind,
                    "simulator_algorithm": "scripted-disclosure-v1",
                    "max_turns": max_turns,
                }
            },
        )

    async def execute(self, session=None, *, run, variant, item):
        del session
        prepared = await self.prepare(run=run, variant=variant, item=item)
        return await prepared.execute()
