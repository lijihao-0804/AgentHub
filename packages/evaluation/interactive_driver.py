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
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Any
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
from packages.evaluation.interactive_lifecycle import (
    INTERACTIVE_DRIVER_VERSION,
    CaseExecutionIdentity,
    InteractiveLifecycle,
)
from packages.evaluation.interactive_observation import (
    observation_envelope,
    observation_from_envelope,
    with_cleanup,
)
from packages.evaluation.interactive_protocol import require_interactive_protocol
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperiment,
    EvaluationExperimentRun,
)
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
                str(k): [str(v) for v in values] for k, values in dict(raw["triggers"]).items()
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
    version = INTERACTIVE_DRIVER_VERSION

    def __init__(
        self,
        scenarios: ScriptedScenarioDriver,
        *,
        context_factory: Callable[[EvaluationExperimentRun], Awaitable[WorkspaceExecutionContext]],
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

    async def prepare(
        self, *, run, variant, item, case_identity: CaseExecutionIdentity | None = None
    ) -> PreparedCaseExecution:
        if item.category != "INTERACTIVE":
            raise ValueError("INTERACTIVE_CATEGORY_REQUIRED")
        context = await self.context_factory(run)
        if str(context.workspace_id) != str(run.workspace_id):
            raise AgentHubError(
                "FORBIDDEN", "The interactive case belongs to another workspace.", 403
            )
        if "evaluation_run" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "Evaluation permission is required.", 403)
        if case_identity is None:
            raise RuntimeError("EVALUATION_CASE_IDENTITY_REQUIRED")
        if (
            case_identity.workspace_id != run.workspace_id
            or case_identity.run_id != run.id
            or case_identity.variant_id != variant.id
            or case_identity.item_id != item.id
        ):
            raise RuntimeError("EVALUATION_CASE_IDENTITY_MISMATCH")
        max_turns, max_tokens = _execution_limits(item.input)
        async with self.scenarios.runtime.session_factory() as session:
            evaluator_manifest = await session.scalar(
                select(EvaluationExperiment.evaluator_manifest).where(
                    EvaluationExperiment.workspace_id == run.workspace_id,
                    EvaluationExperiment.id == run.experiment_id,
                )
            )
        require_interactive_protocol(evaluator_manifest)
        assertions = item.expected.get("assertions")
        if not isinstance(assertions, Mapping):
            raise ValueError("INTERACTIVE_ASSERTIONS_REQUIRED")
        canary = str(item.expected.get("canary") or "")
        environment = self.environment_factory(context, item)
        if environment.kind != "support_fixture" or not hasattr(environment, "for_case"):
            raise RuntimeError("EVALUATION_ENVIRONMENT_DURABILITY_REQUIRED")
        environment = environment.for_case(case_identity)
        lifecycle = InteractiveLifecycle(self.scenarios.runtime.session_factory)
        try:
            episode = await lifecycle.reserve(
                case_identity,
                seed=int(item.input.get("seed", 0)),
                fixture_ref=str(item.input.get("environment_ref", "support_customer")),
                input_hash=canonical_json_hash(item.input),
                policy_hash=canonical_json_hash(dict(item.input["simulator"]["triggers"])),
            )
            handle = await environment.reset(
                seed=int(item.input.get("seed", 0)),
                fixture_ref=str(item.input.get("environment_ref", "support_customer")),
            )
            simulator = simulator_from_item(
                item,
                customer_ref=(
                    environment.customer_ref(handle)
                    if hasattr(environment, "customer_ref")
                    else None
                ),
            )
            opening = (await simulator.opening()).strip()
            await lifecycle.reserve_turn(
                case_identity, turn_index=0, input_hash=canonical_json_hash(opening)
            )
            prepared = await self.scenarios.prepare_first(
                context,
                agent_version_id=variant.agent_version_id,
                first_input=opening,
                knowledge_snapshots=variant.effective_knowledge_snapshots,
                wiki_publications=variant.effective_wiki_publications,
                instance_id=episode.instance_id.hex,
                thread_id=episode.reserved_thread_id,
            )
            await lifecycle.bind_thread(case_identity, thread_id=prepared.thread_id)
            await lifecycle.bind_turn(
                case_identity, turn_index=0, turn_id=prepared.turn_id, run_id=prepared.run_id
            )
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
        except BaseException as error:
            try:
                await lifecycle.terminate(case_identity, reason="INTERACTIVE_PREPARATION_FAILED")
                await lifecycle.cleanup(case_identity)
            except BaseException as cleanup_error:
                error.add_note(
                    "Interactive cleanup remains pending: " + type(cleanup_error).__name__
                )
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
            total_cost = Decimal("0")
            currency: str | None = None
            known_cost = True
            known_tokens = True
            known_cached = True
            known_token_turns = 0
            known_cost_turns = 0
            canary_leaked = False
            prepared_run = prepared
            user_text = first_input
            run_ids: list[UUID] = []

            async def retain_turn(partial_assertion: StateAssertion | None = None) -> None:
                if partial_assertion is not None:
                    turns[-1]["business_assertion"] = {
                        **_safe_assertion(partial_assertion),
                        "source": "support_fixture.assert_state.partial",
                    }
                await lifecycle.record_turn(
                    case_identity, turn_index=turn_index, evidence=turns[-1]
                )

            try:
                for turn_index in range(max_turns):
                    await lifecycle.ensure_current(case_identity)
                    context = await self.context_factory(run)
                    if (
                        str(context.workspace_id) != str(case_identity.workspace_id)
                        or "evaluation_run" not in context.permissions
                    ):
                        raise AgentHubError("FORBIDDEN", "Evaluation access was revoked.", 403)
                    await lifecycle.dispatch(case_identity, turn_index=turn_index)
                    result = await self.scenarios.runtime.execute_prepared_run(
                        context, run_id=prepared_run.run_id
                    )
                    while result.status == "WAITING_APPROVAL":
                        await lifecycle.ensure_current(case_identity)
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
                        if self.approval_context_factory is None:
                            raise AgentHubError(
                                "EVALUATION_APPROVAL_ACTOR_UNAVAILABLE",
                                "A distinct approval operator is required.",
                                409,
                            )
                        approver = await self.approval_context_factory(run)
                        if (
                            approver.workspace_id != context.workspace_id
                            or approver.user_id == context.user_id
                        ):
                            raise AgentHubError("FORBIDDEN", "Approval identity is invalid.", 403)
                        await self.scenarios.runtime.approval_service.decide(
                            approver,
                            approval.id,
                            decision=ApprovalDecisionStatus(approval_decisions[decision_index]),
                        )
                        decision_index += 1
                        await lifecycle.ensure_current(case_identity)
                        result = await self.scenarios.runtime.resume(
                            context, run_id=result.run_id, approval_id=approval.id
                        )
                    if result.status == "WAITING_APPROVAL":
                        await lifecycle.ensure_current(case_identity)
                        result = await self.scenarios.runtime.cancel(context, run_id=result.run_id)
                    async with self.scenarios.runtime.session_factory() as session:
                        persisted = await session.scalar(
                            select(AgentRun).where(
                                AgentRun.workspace_id == case_identity.workspace_id,
                                AgentRun.id == result.run_id,
                            )
                        )
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
                            known_token_turns += 1
                            total_input += input_tokens
                            total_output += output_tokens
                            if cached_tokens is None:
                                known_cached = False
                            else:
                                total_cached += cached_tokens
                        if cost_amount is None or not persisted.cost_currency:
                            known_cost = False
                        else:
                            known_cost_turns += 1
                            total_cost += Decimal(str(cost_amount))
                        if persisted.cost_currency:
                            if currency is not None and currency != persisted.cost_currency:
                                known_cost = False
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
                                "proposed_tool_sequence": [str(item) for item in turn_proposed],
                                "executed_tool_sequence": [str(item) for item in turn_executed],
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
                        await retain_turn()
                        break
                    if result.status in {"FAILED", "CANCELLED", "NEEDS_ATTENTION"}:
                        stop_reason = stop_reason or f"agent_{result.status.lower()}"
                        await retain_turn()
                        if result.status == "NEEDS_ATTENTION":
                            raise AgentHubError(
                                "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED",
                                "The interactive outcome requires durable reconciliation.",
                                409,
                            )
                        break
                    if result.status != "SUCCEEDED":
                        stop_reason = "agent_not_successful"
                        await retain_turn()
                        break
                    if max_tokens is not None and (
                        not known_tokens or total_input + total_output >= max_tokens
                    ):
                        stop_reason = "budget_exhausted" if known_tokens else "budget_unavailable"
                        await retain_turn()
                        break
                    # Assert the world after each turn.  This is what makes a completed
                    # answer insufficient: only the environment can stop the dialogue.
                    turn_assertions = dict(assertions)
                    turn_assertions["turn_count"] = turn_index + 1
                    assertion = await environment.assert_state(handle, turn_assertions)
                    await retain_turn(assertion)
                    min_turns = int(assertions.get("min_turns", 1))
                    approvals_satisfied = decision_index >= len(approval_decisions)
                    if assertion.passed and turn_index + 1 >= min_turns and approvals_satisfied:
                        stop_reason = "goal_met"
                        break
                    if simulator.exhausted() or turn_index + 1 >= max_turns:
                        stop_reason = "max_turns"
                        break
                    user_text = (
                        await simulator.respond(transcript=transcript, agent_message=final_output)
                    ).strip()
                    if not user_text or user_text.startswith("（对话已结束）"):
                        stop_reason = "simulator_exhausted"
                        break
                    await lifecycle.ensure_current(case_identity)
                    reserved_turn = await lifecycle.reserve_turn(
                        case_identity,
                        turn_index=turn_index + 1,
                        input_hash=canonical_json_hash(user_text),
                    )
                    next_turn = await self.scenarios.threads.open_turn(
                        context,
                        prepared.thread_id,
                        user_input=user_text,
                        client_token=reserved_turn.client_token,
                    )
                    next_run = await self.scenarios.runtime.prepare_run(
                        context,
                        agent_version_id=variant.agent_version_id,
                        input_text=user_text,
                        thread_id=prepared.thread_id,
                        thread_turn_id=next_turn.id,
                        execution_overrides=AgentRunExecutionOverrides.for_evaluation(
                            list(variant.effective_knowledge_snapshots),
                            wiki_publications=list(variant.effective_wiki_publications),
                        ),
                    )
                    await self.scenarios.threads.attach_run(
                        context, turn_id=next_turn.id, run_id=next_run.id
                    )
                    await lifecycle.bind_turn(
                        case_identity,
                        turn_index=turn_index + 1,
                        turn_id=next_turn.id,
                        run_id=next_run.id,
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
            except BaseException as error:
                try:
                    await lifecycle.terminate(
                        case_identity, reason="INTERACTIVE_EXECUTION_INTERRUPTED"
                    )
                    await lifecycle.cleanup(case_identity)
                except BaseException as cleanup_error:
                    error.add_note(
                        "Interactive cleanup remains pending: " + type(cleanup_error).__name__
                    )
                raise
            safe_assertion = _safe_assertion(assertion)
            await lifecycle.retain_assertion(
                case_identity, assertion=safe_assertion, source="support_fixture.assert_state"
            )
            observation = {
                "category": "INTERACTIVE",
                "interactive_driver_version": self.version,
                "interactive_case_id": str(case_identity.case_id),
                "repetition_index": case_identity.repetition_index,
                "observed_agent_status": turns[0]["status"] if turns else None,
                "observed_agent_failure_code": turns[0]["failure_code"] if turns else None,
                "terminal_agent_run_id": turns[-1]["run_id"] if turns else None,
                "terminal_agent_status": turns[-1]["status"] if turns else None,
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
                "cost_amount": str(total_cost) if known_cost else None,
                "cost_currency": currency if known_cost else None,
                "total_tokens": total_input + total_output if known_tokens else None,
                "usage_coverage": {
                    "executed_turns": len(turns),
                    "known_token_turns": known_token_turns,
                    "known_cost_turns": known_cost_turns,
                },
            }
            result = CaseExecutionObservation(
                observation=observation,
                agent_run_id=run_ids[0] if run_ids else prepared.run_id,
                latency_ms=round((perf_counter() - started) * 1000),
                input_tokens=total_input if known_tokens else None,
                output_tokens=total_output if known_tokens else None,
                cached_tokens=total_cached if known_tokens and known_cached else None,
                total_tokens=total_input + total_output if known_tokens else None,
                cost_amount=total_cost if known_cost else None,
                cost_currency=currency if known_cost else None,
                aggregate_billing_complete=known_tokens and known_cost and known_cached,
            )
            envelope = observation_envelope(result)
            await lifecycle.retain_observation(case_identity, envelope=envelope)
            try:
                await environment.teardown(handle)
                teardown_status = "TEARDOWN_COMPLETE"
            except Exception as error:
                teardown_status = (
                    "CLEANUP_BLOCKED"
                    if getattr(error, "code", None) == "CLEANUP_BLOCKED"
                    else "TEARDOWN_FAILED"
                )
                teardown_error = getattr(error, "code", None) or type(error).__name__
            return observation_from_envelope(
                with_cleanup(envelope, teardown_status, teardown_error)
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

    async def execute(self, session=None, *, run, variant, item, case_identity=None):
        del session
        prepared = await self.prepare(
            run=run, variant=variant, item=item, case_identity=case_identity
        )
        return await prepared.execute()


def _execution_limits(value: Mapping[str, Any]) -> tuple[int, int | None]:
    simulator = value.get("simulator")
    if not isinstance(simulator, Mapping):
        raise ValueError("INTERACTIVE_SIMULATOR_CONFIG_REQUIRED")
    declared = [
        value.get("max_turns", simulator.get("max_turns", 4)),
        simulator.get("max_turns", 4),
    ]
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10
        for limit in declared
    ):
        raise ValueError("INTERACTIVE_TURN_LIMIT_INVALID")
    tokens = value.get("max_tokens")
    if tokens is not None and (
        isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0
    ):
        raise ValueError("INTERACTIVE_TOKEN_LIMIT_INVALID")
    if value.get("environment_ref") != "support_customer":
        raise ValueError("INTERACTIVE_ENVIRONMENT_NOT_REGISTERED")
    seed = value.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
        raise ValueError("INTERACTIVE_SEED_INVALID")
    if "max_cost_cny" in value:
        try:
            cost = Decimal(str(value["max_cost_cny"]))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("INTERACTIVE_COST_LIMIT_INVALID") from None
        if not cost.is_finite() or cost < 0:
            raise ValueError("INTERACTIVE_COST_LIMIT_INVALID")
        # There is currently no episode-wide reservation covering auxiliary
        # calls. Never turn a finite declared limit into an unenforced estimate.
        raise AgentHubError(
            "INTERACTIVE_COST_RESERVATION_UNAVAILABLE",
            "A finite interactive cost limit requires complete dispatch reservations.",
            409,
        )
    return min(declared), tokens


def _safe_assertion(assertion: StateAssertion) -> dict[str, Any]:
    return {
        "passed": assertion.passed,
        "reason": assertion.reason,
        "observed": {
            "ticket_count": assertion.observed.get("ticket_count"),
            "ticket_statuses": assertion.observed.get("ticket_statuses", []),
        },
    }
