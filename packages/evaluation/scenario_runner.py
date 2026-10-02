"""Formal multi-turn case adapter with injected, workspace-scoped fixture creation."""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from packages.agent_runtime.models import RunStep
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperimentRun,
    EvaluationExperimentVariant,
)
from packages.evaluation.runner import CaseExecutionObservation, PreparedCaseExecution
from packages.evaluation.scenario_contracts import ScenarioExpected, ScenarioInput
from packages.evaluation.scenario_driver import ScriptedScenarioDriver
from packages.tools.models import Customer, Ticket


class ScenarioEvaluationDriver:
    """Labels score persisted facts; only scripted user turns enter the model.

    The caller must provide an isolated customer per invocation. Fixture creation is
    injected rather than granting evaluation callers implicit business write access.
    A lost case lease fails closed in ExperimentRunner; this adapter never replays it.
    """

    def __init__(
        self,
        scenarios: ScriptedScenarioDriver,
        context_factory: Callable[[EvaluationExperimentRun], Awaitable[WorkspaceExecutionContext]],
        fixture_factory: Callable[[WorkspaceExecutionContext], Awaitable[UUID]],
        *,
        approval_context_factory: Callable[
            [EvaluationExperimentRun], Awaitable[WorkspaceExecutionContext]
        ]
        | None = None,
    ):
        self.scenarios = scenarios
        self.context_factory = context_factory
        self.fixture_factory = fixture_factory
        self.approval_context_factory = approval_context_factory

    async def prepare(self, *, run, variant, item) -> PreparedCaseExecution:
        context = await self.context_factory(run)
        if str(context.workspace_id) != str(run.workspace_id):
            raise AgentHubError("FORBIDDEN", "The scenario belongs to another workspace.", 403)
        if "evaluation_run" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "Evaluation permission is required.", 403)
        if item.category != "MULTI_STEP":
            raise ValueError("SCENARIO_CATEGORY_REQUIRED")
        script = ScenarioInput.model_validate(item.input.get("scenario"))
        expected = ScenarioExpected.model_validate(item.expected.get("scenario"))
        approver = (
            await self.approval_context_factory(run)
            if expected.approval_decisions and self.approval_context_factory is not None
            else context
        )
        if item.input.get("task") != script.user_turns[0]:
            raise ValueError("SCENARIO_TASK_MUST_MATCH_FIRST_TURN")
        customer_id = await self.fixture_factory(context)
        workspace_id = UUID(context.workspace_id)
        async with self.scenarios.runtime.session_factory() as session:
            customer = await session.scalar(
                select(Customer).where(
                    Customer.id == customer_id, Customer.workspace_id == workspace_id
                )
            )
            tickets = list(
                await session.scalars(
                    select(Ticket.id).where(
                        Ticket.customer_id == customer_id, Ticket.workspace_id == workspace_id
                    )
                )
            )
            if customer is None or tickets:
                raise ValueError("SCENARIO_FRESH_CUSTOMER_REQUIRED")
            turns = [
                t.replace("fixture_customer_ref", customer.customer_ref) for t in script.user_turns
            ]
        handle = await self.scenarios.prepare_first(
            context,
            agent_version_id=variant.agent_version_id,
            first_input=turns[0],
            knowledge_snapshots=variant.effective_knowledge_snapshots,
        )

        async def execute():
            observed = await self.scenarios.execute(
                context,
                case_id=item.case_key,
                agent_version_id=variant.agent_version_id,
                user_turns=turns,
                knowledge_snapshots=variant.effective_knowledge_snapshots,
                approval_decisions=expected.approval_decisions,
                prepared_first=handle,
                approval_context=approver,
            )
            run_ids = [UUID(t["run_id"]) for t in observed["turns"]]
            sequence = []
            async with self.scenarios.runtime.session_factory() as session:
                for run_id in run_ids:
                    proposals = await session.scalars(
                        select(RunStep)
                        .where(
                            RunStep.workspace_id == workspace_id,
                            RunStep.agent_run_id == run_id,
                            RunStep.kind == "TOOL_PROPOSAL",
                        )
                        .order_by(RunStep.sequence_number)
                    )
                    for proposal in proposals:
                        sequence.extend(proposal.safe_metadata.get("tool_identities", []))
                tickets = list(
                    await session.scalars(
                        select(Ticket).where(
                            Ticket.workspace_id == workspace_id,
                            Ticket.customer_id == customer_id,
                        )
                    )
                )
            failures = []
            if len(tickets) != expected.ticket_count:
                failures.append("ticket_count")
            if any(t.status != "OPEN" for t in tickets):
                failures.append("ticket_status")
            if not observed["execution_complete"]:
                failures.append("scenario_incomplete")
            if observed["unused_approval_decision_count"]:
                failures.append("approval_branch_not_exercised")
            if not set(expected.required_tools).issubset(sequence):
                failures.append("required_tools")
            if set(expected.forbidden_tools).intersection(sequence):
                failures.append("forbidden_tools")
            observed.update(
                category="MULTI_STEP",
                steps=sequence,
                terminal_status=observed["turns"][-1]["status"],
                business_outcome={
                    "evaluator_version": "support-scenario-postcondition-v2",
                    "source": "persisted_runs_proposals_and_isolated_customer_tickets",
                    "status": "AVAILABLE",
                    "success": not failures,
                    "ticket_count": len(tickets),
                    "failed_checks": failures,
                },
            )
            rows = observed["turns"]

            def total(key):
                return sum(t[key] for t in rows) if all(t[key] is not None for t in rows) else None

            amounts = [t["cost_amount"] for t in rows]
            currencies = {t["cost_currency"] for t in rows}
            known_cost = (
                all(a is not None for a in amounts)
                and len(currencies) == 1
                and None not in currencies
            )
            input_tokens, output_tokens = total("input_tokens"), total("output_tokens")
            return CaseExecutionObservation(
                observation=observed,
                agent_run_id=handle.run_id,
                latency_ms=round(observed["latency_ms"]),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=total("cached_tokens"),
                total_tokens=input_tokens + output_tokens
                if input_tokens is not None and output_tokens is not None
                else None,
                cost_amount=sum((Decimal(a) for a in amounts), Decimal(0)) if known_cost else None,
                cost_currency=next(iter(currencies)) if known_cost else None,
            )

        return PreparedCaseExecution(
            handle.run_id,
            execute,
            preparation_observation={
                "scenario_preparation": {
                    "driver_version": "scripted-scenario-v2",
                    "instance_id": handle.instance_id,
                    "thread_id": str(handle.thread_id),
                    "first_run_id": str(handle.run_id),
                    "customer_id": str(customer_id),
                    "planned_turn_count": len(turns),
                }
            },
        )

    async def execute(
        self,
        session=None,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ):
        del session
        return await (await self.prepare(run=run, variant=variant, item=item)).execute()
