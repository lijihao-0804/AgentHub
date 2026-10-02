"""Support postconditions over persistent facts, independent of model answer text."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.models import AgentRun
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.tools.models import Ticket


@dataclass(frozen=True)
class SupportPostcondition:
    workspace_id: UUID
    customer_id: UUID
    action_keys: tuple[str, ...]
    expected_ticket_count: int
    allowed_ticket_statuses: tuple[str, ...] = ("OPEN",)
    allowed_run_statuses: tuple[str, ...] = ("SUCCEEDED",)

    def __post_init__(self) -> None:
        if not self.action_keys or len(set(self.action_keys)) != len(self.action_keys):
            raise ValueError("SCENARIO_ACTION_SCOPE_REQUIRED")
        if type(self.expected_ticket_count) is not int or self.expected_ticket_count < 0:
            raise ValueError("INVALID_EXPECTED_TICKET_COUNT")
        if not self.allowed_run_statuses or not self.allowed_ticket_statuses:
            raise ValueError("SCENARIO_STATUSES_REQUIRED")


async def inspect_support_outcome(
    session: AsyncSession,
    context: WorkspaceExecutionContext,
    condition: SupportPostcondition,
    *,
    run_id: UUID | None,
) -> dict[str, object]:
    if "evaluation_run" not in context.permissions:
        raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
    if str(condition.workspace_id) != context.workspace_id:
        raise AgentHubError("FORBIDDEN", "The scenario belongs to another workspace.", 403)
    run_status = (
        await session.scalar(
            select(AgentRun.status).where(
                AgentRun.workspace_id == condition.workspace_id, AgentRun.id == run_id
            )
        )
        if run_id is not None
        else None
    )
    rows = list(
        (
            await session.execute(
                select(Ticket.customer_id, Ticket.status).where(
                    Ticket.workspace_id == condition.workspace_id,
                    Ticket.idempotency_key.in_(condition.action_keys),
                )
            )
        ).all()
    )
    failures = []
    if len(rows) != condition.expected_ticket_count:
        failures.append("ticket_count")
    if any(row.customer_id != condition.customer_id for row in rows):
        failures.append("customer_identity")
    if any(row.status not in condition.allowed_ticket_statuses for row in rows):
        failures.append("ticket_status")
    if run_status is not None and run_status not in condition.allowed_run_statuses:
        failures.append("run_status")
    return {
        "evaluator_version": "support-postcondition-v1",
        "source": "workspace_scoped_ticket_query",
        "status": "NOT_AVAILABLE" if run_status is None else "AVAILABLE",
        "success": None if run_status is None else not failures,
        "missing_reason": "run_status_missing" if run_status is None else None,
        "ticket_count": len(rows),
        "failed_checks": failures,
    }


def support_condition_from_expected(
    expected: dict, *, workspace_id: str
) -> SupportPostcondition | None:
    value = expected.get("business_postcondition")
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("evaluator_version") != "support-postcondition-v1":
        raise ValueError("INVALID_BUSINESS_POSTCONDITION")
    keys = value.get("action_keys")
    if not isinstance(keys, list) or not all(
        isinstance(key, str) and 0 < len(key) <= 128 for key in keys
    ):
        raise ValueError("INVALID_SCENARIO_ACTION_KEYS")
    statuses = value.get("allowed_run_statuses", ["SUCCEEDED"])
    ticket_statuses = value.get("allowed_ticket_statuses", ["OPEN"])
    if (
        not isinstance(statuses, list)
        or not all(isinstance(s, str) for s in statuses)
        or not set(statuses)
        <= {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "NEEDS_ATTENTION",
        }
    ):
        raise ValueError("INVALID_SCENARIO_RUN_STATUSES")
    if not isinstance(ticket_statuses, list) or not all(
        isinstance(s, str) and s for s in ticket_statuses
    ):
        raise ValueError("INVALID_SCENARIO_TICKET_STATUSES")
    customer_id = value.get("customer_id")
    if not isinstance(customer_id, str):
        raise ValueError("INVALID_SCENARIO_CUSTOMER_ID")
    return SupportPostcondition(
        workspace_id=UUID(workspace_id),
        customer_id=UUID(customer_id),
        action_keys=tuple(keys),
        expected_ticket_count=value.get("expected_ticket_count"),
        allowed_run_statuses=tuple(statuses),
        allowed_ticket_statuses=tuple(ticket_statuses),
    )


@dataclass(frozen=True)
class SupportScenario:
    """Contract for scripted multi-turn tasks; the driver arrives in M-I3."""

    case_id: str
    user_turns: tuple[str, ...]
    postcondition: SupportPostcondition
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    precedence: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.case_id
            or not self.user_turns
            or any(not turn.strip() for turn in self.user_turns)
        ):
            raise ValueError("SCENARIO_TURNS_REQUIRED")
        if set(self.required_tools).intersection(self.forbidden_tools):
            raise ValueError("SCENARIO_TOOL_CONSTRAINT_CONFLICT")

    def accepts_trajectory(self, observed: tuple[str, ...] | None) -> bool | None:
        if observed is None:
            return None
        if not set(self.required_tools).issubset(observed) or set(
            self.forbidden_tools
        ).intersection(observed):
            return False
        return all(
            before in observed
            and after in observed
            and observed.index(before) < observed.index(after)
            for before, after in self.precedence
        )
