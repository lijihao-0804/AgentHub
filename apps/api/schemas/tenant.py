from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OrganizationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str


class OrganizationMemberCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role: Literal["OWNER", "ADMIN", "MEMBER"]


class OrganizationMemberUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["OWNER", "ADMIN", "MEMBER"]


class OrganizationMemberResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    email: str
    role: str


class WorkspaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    name: str = Field(min_length=1, max_length=200)


class WorkspaceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    organization_id: UUID
    name: str


class WorkspaceMemberCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role: Literal["DEVELOPER", "VIEWER"]


class WorkspaceMemberUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["DEVELOPER", "VIEWER"]


class WorkspaceMemberResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    email: str
    role: str
