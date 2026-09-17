from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PrincipalContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    user_id: str | None = None


class OrganizationContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: PrincipalContext
    organization_id: str = Field(min_length=1)
    org_role: str = Field(min_length=1)


class WorkspaceExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization: OrganizationContext
    workspace_id: str = Field(min_length=1)
    workspace_role: str = Field(min_length=1)
    permissions: frozenset[str] = frozenset()

    @property
    def request_id(self) -> str:
        return self.organization.principal.request_id

    @property
    def user_id(self) -> str | None:
        return self.organization.principal.user_id
