from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelRetryPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=5)


class RetrievalConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embedding_model: str | None = None
    reranker_model: str | None = None
    dense_top_k: int | None = Field(default=None, ge=1)
    sparse_top_k: int | None = Field(default=None, ge=1)
    candidate_top_k: int | None = Field(default=None, ge=1)
    final_top_k: int | None = Field(default=None, ge=1)


class ContextBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reserved_output_tokens: int | None = Field(default=None, ge=1)
    max_retrieval_tokens: int | None = Field(default=None, ge=1)
    max_tool_result_tokens: int | None = Field(default=None, ge=1)


class RuntimeConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int | None = Field(default=None, ge=1, le=32)
    max_tool_calls: int | None = Field(default=None, ge=1, le=64)
    max_identical_calls: int | None = Field(default=None, ge=1, le=4)
    max_parallel_reads: int | None = Field(default=None, ge=1, le=8)
    context_budget: ContextBudgetRequest | None = None


class AgentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    system_prompt: str = Field(min_length=1)
    prompt_version: int = Field(default=1, ge=1)
    model_profile_id: UUID
    knowledge_binding_mode: Literal["PINNED", "LATEST"] = "PINNED"
    model_retry_policy: ModelRetryPolicyRequest = Field(default_factory=ModelRetryPolicyRequest)
    retrieval_config: RetrievalConfigRequest = Field(default_factory=RetrievalConfigRequest)
    runtime_config: RuntimeConfigRequest = Field(default_factory=RuntimeConfigRequest)


class AgentPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    system_prompt: str | None = Field(default=None, min_length=1)
    prompt_version: int | None = Field(default=None, ge=1)
    model_profile_id: UUID | None = None
    knowledge_binding_mode: Literal["PINNED", "LATEST"] | None = None
    model_retry_policy: ModelRetryPolicyRequest | None = None
    retrieval_config: RetrievalConfigRequest | None = None
    runtime_config: RuntimeConfigRequest | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    description: str | None
    system_prompt: str
    prompt_version: int
    model_profile_id: UUID
    knowledge_binding_mode: str
    model_retry_policy: dict[str, Any]
    retrieval_config: dict[str, Any]
    runtime_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class AgentVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    agent_id: UUID
    version_number: int
    spec_schema_version: int
    resolved_spec: dict[str, Any]
    resolved_spec_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime
    created_by: UUID


class AgentPublishResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_version_id: UUID
    agent_id: UUID
    version_number: int
    resolved_spec_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime
