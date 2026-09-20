from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderCredentialCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    secret: str = Field(min_length=1)
    base_url: str | None = None
    enabled: bool = True


class ProviderCredentialPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = None
    enabled: bool | None = None


class ProviderCredentialRotateSecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str = Field(min_length=1)


class ProviderCredentialResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    provider: str
    name: str
    base_url: str | None
    enabled: bool
    created_at: datetime


class ModelProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_credential_id: UUID
    model: str = Field(min_length=1, max_length=128)
    temperature: Decimal = Field(default=Decimal("0"), ge=0)
    max_tokens: int = Field(gt=0)
    timeout_seconds: Decimal = Field(gt=0)
    fallback_profile_id: UUID | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ModelProfilePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_credential_id: UUID | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    temperature: Decimal | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: Decimal | None = Field(default=None, gt=0)
    fallback_profile_id: UUID | None = None
    capabilities: dict[str, Any] | None = None
    enabled: bool | None = None


class ModelProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    provider_credential_id: UUID
    model: str
    temperature: Decimal
    max_tokens: int
    timeout_seconds: Decimal
    fallback_profile_id: UUID | None
    capabilities: dict[str, Any]
    enabled: bool
    created_at: datetime


class ModelProfileTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_profile_id: UUID
    status: Literal["healthy", "degraded", "unavailable"]
    failure_code: str | None
    latency_ms: float = Field(ge=0)


class ToolCatalogEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: str
    description: str
    input_schema: dict[str, Any]
    effect: Literal["READ", "WRITE"]
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    approval_policy: Literal["NEVER", "ALWAYS"]
    timeout_seconds: int
    execution_kind: Literal["builtin", "action"]


class ToolCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    builtin_identity: str | None = Field(default=None, min_length=1, max_length=128)
    identity: str | None = Field(default=None, min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> ToolCreateRequest:
        if (self.builtin_identity is None) == (self.identity is None):
            raise ValueError("exactly one builtin tool identity is required")
        return self

    @property
    def resolved_identity(self) -> str:
        return self.builtin_identity or self.identity  # type: ignore[return-value]


class ToolPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    enabled: bool | None = None


class ToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    name: str
    description: str | None
    enabled: bool
    created_at: datetime
    identity: str | None
    effect: Literal["READ", "WRITE"] | None
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] | None
    approval_policy: Literal["NEVER", "ALWAYS"] | None
    execution_kind: Literal["builtin", "action", "mcp"] | None
    source_kind: Literal["builtin", "mcp"] | None = None
    current_revision_id: UUID | None
    current_revision_number: int | None
    current_spec_hash: str | None


class ToolRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    tool_id: UUID
    revision_number: int
    spec: dict[str, Any]
    spec_hash: str
    created_at: datetime
    created_by: UUID


class AgentKnowledgeBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_base_id: UUID
    binding_mode: Literal["PINNED", "LATEST"]
    snapshot_id: UUID | None = None


class AgentKnowledgeBindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    agent_id: UUID
    knowledge_base_id: UUID
    binding_mode: Literal["PINNED", "LATEST"]
    snapshot_id: UUID | None


class AgentToolBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: UUID
    tool_revision_id: UUID | None = None


class AgentToolBindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    agent_id: UUID
    tool_id: UUID
    tool_revision_id: UUID | None


class KnowledgeDocumentManagementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    knowledge_base_id: UUID
    name: str
    created_at: datetime
    current_revision_id: UUID | None
    current_revision_number: int | None
    current_revision_status: str | None
    current_revision_lifecycle_status: str | None
    current_revision_created_at: datetime | None


class KnowledgeSnapshotManagementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    knowledge_base_id: UUID
    content_hash: str = Field(min_length=64, max_length=64)
    snapshot_schema_version: int = Field(ge=1)
    item_count: int = Field(ge=0)
    created_at: datetime


class KnowledgeSnapshotItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    document_revision_id: UUID
    document_name: str
    revision_number: int
    ingestion_status: str


class KnowledgeSnapshotDetailResponse(KnowledgeSnapshotManagementResponse):
    items: list[KnowledgeSnapshotItemResponse]


__all__ = [
    "AgentKnowledgeBindingRequest",
    "AgentKnowledgeBindingResponse",
    "AgentToolBindingRequest",
    "AgentToolBindingResponse",
    "KnowledgeDocumentManagementResponse",
    "KnowledgeSnapshotDetailResponse",
    "KnowledgeSnapshotItemResponse",
    "KnowledgeSnapshotManagementResponse",
    "ModelProfileCreateRequest",
    "ModelProfilePatchRequest",
    "ModelProfileResponse",
    "ModelProfileTestResponse",
    "ProviderCredentialCreateRequest",
    "ProviderCredentialPatchRequest",
    "ProviderCredentialResponse",
    "ProviderCredentialRotateSecretRequest",
    "ToolCatalogEntryResponse",
    "ToolCreateRequest",
    "ToolPatchRequest",
    "ToolResponse",
    "ToolRevisionResponse",
]
