from __future__ import annotations

from typing import TypedDict
from uuid import UUID

from sqlalchemy import Row, Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.audit import append_audit
from packages.control_plane.rbac import SUPPORT_RECORDS_MANAGE, SUPPORT_RECORDS_READ
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.tools.models import OPEN_TICKET_STATUSES, Customer, Ticket


class Page[T](TypedDict):
    items: list[T]
    total: int


type TicketRow = Row[tuple[Ticket, str, str]]


def _workspace(context: WorkspaceExecutionContext, permission: str) -> UUID:
    if permission not in context.permissions or context.user_id is None:
        raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
    return UUID(context.workspace_id)


def _audit(
    session: AsyncSession,
    context: WorkspaceExecutionContext,
    action: str,
    resource_type: str,
    resource_id: UUID,
    metadata: dict[str, str | int] | None = None,
) -> None:
    append_audit(
        session,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        actor_user_id=UUID(context.user_id),
        organization_id=UUID(context.organization.organization_id),
        workspace_id=UUID(context.workspace_id),
        request_id=context.request_id,
        safe_metadata=metadata,
    )


def _customers(workspace_id: UUID) -> Select[tuple[Customer]]:
    return select(Customer).where(
        Customer.workspace_id == workspace_id, Customer.is_evaluation_fixture.is_(False)
    )


async def _page[T](
    session: AsyncSession,
    query: Select[tuple[T]],
    limit: int,
    offset: int,
) -> Page[T]:
    total = (
        await session.execute(select(func.count()).select_from(query.order_by(None).subquery()))
    ).scalar_one()
    items = await session.scalars(query.limit(limit).offset(offset))
    return {"items": list(items), "total": total}


async def list_customers(
    session: AsyncSession, context: WorkspaceExecutionContext, *, q: str, limit: int, offset: int
) -> Page[Customer]:
    query = _customers(_workspace(context, SUPPORT_RECORDS_READ))
    needle = q.strip()
    if needle:
        query = query.where(
            or_(
                Customer.customer_ref.icontains(needle, autoescape=True),
                Customer.name.icontains(needle, autoescape=True),
                Customer.email.icontains(needle, autoescape=True),
            )
        )
    return await _page(
        session, query.order_by(Customer.created_at.desc(), Customer.id), limit, offset
    )


async def register_customer(
    session: AsyncSession,
    context: WorkspaceExecutionContext,
    *,
    customer_ref: str,
    name: str,
    email: str | None,
) -> tuple[Customer, bool]:
    workspace_id = _workspace(context, SUPPORT_RECORDS_MANAGE)
    customer = await session.scalar(
        insert(Customer)
        .values(workspace_id=workspace_id, customer_ref=customer_ref, name=name, email=email)
        .on_conflict_do_nothing(constraint="uq_customers_workspace_ref")
        .returning(Customer)
    )
    if customer is None:
        existing = await session.scalar(
            _customers(workspace_id).where(Customer.customer_ref == customer_ref)
        )
        if existing is not None and (existing.name, existing.email) == (name, email):
            return existing, False
        raise AgentHubError("CUSTOMER_REF_CONFLICT", "The customer reference is unavailable.", 409)
    _audit(session, context, "support.customer.created", "customer", customer.id)
    await session.commit()
    return customer, True


async def list_tickets(
    session: AsyncSession,
    context: WorkspaceExecutionContext,
    customer_id: UUID,
    *,
    limit: int,
    offset: int,
) -> Page[Ticket]:
    workspace_id = _workspace(context, SUPPORT_RECORDS_READ)
    customer = await session.scalar(_customers(workspace_id).where(Customer.id == customer_id))
    if customer is None:
        raise AgentHubError("CUSTOMER_NOT_FOUND", "The customer was not found.", 404)
    query = select(Ticket).where(
        Ticket.workspace_id == workspace_id, Ticket.customer_id == customer_id
    )
    return await _page(session, query.order_by(Ticket.created_at.desc(), Ticket.id), limit, offset)


async def _ticket(
    session: AsyncSession, workspace_id: UUID, ticket_id: UUID, *, for_update: bool = False
) -> TicketRow:
    query = (
        select(Ticket, Customer.customer_ref, Customer.name)
        .join(
            Customer,
            (Customer.workspace_id == Ticket.workspace_id) & (Customer.id == Ticket.customer_id),
        )
        .where(
            Ticket.workspace_id == workspace_id,
            Ticket.id == ticket_id,
            Customer.is_evaluation_fixture.is_(False),
        )
    )
    if for_update:
        query = query.with_for_update(of=Ticket)
    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise AgentHubError("TICKET_NOT_FOUND", "The ticket was not found.", 404)
    return row


async def ticket_detail(
    session: AsyncSession, context: WorkspaceExecutionContext, ticket_id: UUID
) -> TicketRow:
    return await _ticket(session, _workspace(context, SUPPORT_RECORDS_READ), ticket_id)


async def change_status(
    session: AsyncSession,
    context: WorkspaceExecutionContext,
    ticket_id: UUID,
    *,
    expected_version: int,
    status: str,
) -> TicketRow:
    workspace_id = _workspace(context, SUPPORT_RECORDS_MANAGE)
    row = await _ticket(session, workspace_id, ticket_id, for_update=True)
    ticket = row[0]
    if ticket.status == status and ticket.version in (expected_version, expected_version + 1):
        return row
    if ticket.version != expected_version:
        raise AgentHubError(
            "TICKET_VERSION_CONFLICT", "The ticket changed. Refresh and retry.", 409
        )
    if ticket.status not in OPEN_TICKET_STATUSES or status not in {
        "PENDING",
        "IN_PROGRESS",
        "CLOSED",
    }:
        raise AgentHubError(
            "TICKET_TRANSITION_INVALID", "This ticket transition is unavailable.", 409
        )
    previous = ticket.status
    ticket.status = status
    ticket.version += 1
    _audit(
        session,
        context,
        "support.ticket.status_changed",
        "ticket",
        ticket.id,
        {"previous_status": previous, "status": status, "version": ticket.version},
    )
    await session.commit()
    return row
