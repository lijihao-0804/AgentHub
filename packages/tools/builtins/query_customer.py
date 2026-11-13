"""Workspace-scoped customer and open-ticket lookup."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select

from packages.tools.contracts import ToolDefinition, ToolExecutionContext, ToolSessionFactory
from packages.tools.errors import ToolHandlerError
from packages.tools.models import Customer, Ticket

_OPEN_TICKET_LIMIT = 20
_OPEN_TICKET_STATUSES = ("OPEN", "PENDING", "IN_PROGRESS")


async def query_customer(
    context: ToolExecutionContext,
    definition: ToolDefinition,
    arguments: Mapping[str, Any],
    session_factory: ToolSessionFactory | None,
) -> dict[str, Any]:
    del definition
    if session_factory is None:
        raise ToolHandlerError("TOOL_EXECUTION_FAILED", "The customer lookup is unavailable.")
    try:
        workspace_id = UUID(context.workspace_id)
    except ValueError:
        raise ToolHandlerError(
            "TOOL_EXECUTION_FAILED", "The workspace context is invalid."
        ) from None
    customer_ref = arguments.get("customer_ref")
    if not isinstance(customer_ref, str) or not customer_ref.strip():
        raise ToolHandlerError("CUSTOMER_NOT_FOUND", "The customer was not found.")
    async with session_factory() as session:
        customer = await session.scalar(
            select(Customer).where(
                Customer.workspace_id == workspace_id,
                Customer.customer_ref == customer_ref,
            )
        )
        if customer is None:
            raise ToolHandlerError("CUSTOMER_NOT_FOUND", "The customer was not found.")

        output: dict[str, Any] = {
            "customer_ref": customer.customer_ref,
            "name": customer.name,
            "email": customer.email,
        }
        subject = arguments.get("open_ticket_subject")
        if arguments.get("include_open_tickets") or subject is not None:
            query = select(Ticket).where(
                Ticket.workspace_id == workspace_id,
                Ticket.customer_id == customer.id,
                Ticket.status.in_(_OPEN_TICKET_STATUSES),
            )
            if subject is not None:
                query = query.where(Ticket.subject == subject)
            query = query.order_by(Ticket.ticket_ref).limit(_OPEN_TICKET_LIMIT + 1)
            tickets = list(await session.scalars(query))
            output["open_ticket_scope"] = {
                "subject_filter": "EXACT" if subject is not None else "ALL",
                "subject": subject,
                "statuses": list(_OPEN_TICKET_STATUSES),
                "includes_history": False,
            }
            output["open_tickets_complete"] = len(tickets) <= _OPEN_TICKET_LIMIT
            output["open_tickets"] = [
                {
                    "ticket_ref": ticket.ticket_ref,
                    "subject": ticket.subject,
                    "status": ticket.status,
                }
                for ticket in tickets[:_OPEN_TICKET_LIMIT]
            ]
    return output


__all__ = ["query_customer"]
