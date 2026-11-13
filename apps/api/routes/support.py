from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from apps.api.schemas.support import (
    CustomerPage,
    CustomerRecord,
    RegisterCustomer,
    TicketDetail,
    TicketPage,
    TicketRecord,
    TicketStatusChange,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.support import service

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/support", tags=["support"])
Context = Annotated[WorkspaceExecutionContext, Depends(get_workspace_context)]
Session = Annotated[AsyncSession, Depends(get_db_session)]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=2147483647)]


@router.get("/customers", response_model=CustomerPage)
async def list_customers(
    context: Context,
    session: Session,
    q: Annotated[str, Query(max_length=200, pattern=r"^[^\x00]*$")] = "",
    limit: Limit = 50,
    offset: Offset = 0,
):
    return await service.list_customers(session, context, q=q, limit=limit, offset=offset)


@router.post(
    "/customers",
    response_model=CustomerRecord,
    status_code=201,
    responses={200: {"model": CustomerRecord}},
)
async def register_customer(
    payload: RegisterCustomer, response: Response, context: Context, session: Session
):
    record, created = await service.register_customer(session, context, **payload.model_dump())
    if not created:
        response.status_code = status.HTTP_200_OK
    return record


@router.get("/customers/{customer_id}/tickets", response_model=TicketPage)
async def list_tickets(
    customer_id: UUID, context: Context, session: Session, limit: Limit = 50, offset: Offset = 0
):
    return await service.list_tickets(session, context, customer_id, limit=limit, offset=offset)


def _detail(row: service.TicketRow) -> TicketDetail:
    ticket, customer_ref, customer_name = row
    return TicketDetail(
        **TicketRecord.model_validate(ticket).model_dump(),
        customer_ref=customer_ref,
        customer_name=customer_name,
    )


@router.get("/tickets/{ticket_id}", response_model=TicketDetail)
async def ticket_detail(ticket_id: UUID, context: Context, session: Session):
    return _detail(await service.ticket_detail(session, context, ticket_id))


@router.patch("/tickets/{ticket_id}", response_model=TicketDetail)
async def change_status(
    ticket_id: UUID, payload: TicketStatusChange, context: Context, session: Session
):
    row = await service.change_status(session, context, ticket_id, **payload.model_dump())
    return _detail(row)
