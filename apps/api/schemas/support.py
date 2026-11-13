from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

CustomerRef = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$"
    ),
]
CustomerName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$"
    ),
]
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        max_length=320,
        pattern=r"^[^\s@\x00-\x1f\x7f]+@[^\s@\x00-\x1f\x7f]+\.[^\s@\x00-\x1f\x7f]+$",
    ),
]


class RegisterCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_ref: CustomerRef
    name: CustomerName
    email: Email | None = None

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, email: str | None) -> str | None:
        return email.casefold() if isinstance(email, str) else email


class CustomerRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    customer_ref: str
    name: str
    email: str | None
    created_at: datetime


class CustomerPage(BaseModel):
    items: list[CustomerRecord]
    total: int


class TicketRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ticket_ref: str
    customer_id: UUID
    subject: str
    status: str
    created_at: datetime
    version: int


class TicketDetail(TicketRecord):
    customer_ref: str
    customer_name: str


class TicketPage(BaseModel):
    items: list[TicketRecord]
    total: int


class TicketStatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(gt=0, strict=True)
    status: Literal["IN_PROGRESS", "PENDING", "CLOSED"]
