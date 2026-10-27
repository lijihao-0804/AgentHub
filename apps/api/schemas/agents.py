from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from packages.agent_runtime.context_policy import normalize_context_policy_request
from packages.agent_runtime.runtime_config import (
    MAX_RUN_COST_LIMIT_MICRO_USD,
    MAX_RUNTIME_LIMITS,
)


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
    rerank_top_k: int | None = Field(default=None, ge=1, le=100, strict=True)
    max_chunks_per_document: int | None = Field(default=None, ge=1, le=100, strict=True)
    context_budget_bytes: int | None = Field(default=None, ge=2, le=64_000, strict=True)
    evidence_projection: Literal["compact_v1"] | None = None
    evidence_set_selector: Literal["onyx_v1"] | None = None


class ContextBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reserved_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    max_retrieval_tokens: int | None = Field(default=None, ge=1, strict=True)
    max_tool_result_tokens: int | None = Field(default=None, ge=1, strict=True)


class MemoryConfigRequest(BaseModel):
    """Per-agent memory switches.

    Both default off, and the whole block is dropped from the published spec
    when neither is on, so an agent that never asks for memory publishes the
    same bytes it published before this field existed.
    """

    model_config = ConfigDict(extra="forbid")

    thread_history_search: bool = False
    long_term_memory: bool = False
    max_memory_tokens: int | None = Field(default=None, ge=1, strict=True)
    max_memory_tokens_source: Literal["UNSET", "DEFAULT", "CUSTOM"] | None = None

    @model_validator(mode="after")
    def validate_budget_intent(self) -> MemoryConfigRequest:
        source = self.max_memory_tokens_source
        if source is None:
            source = "CUSTOM" if self.max_memory_tokens is not None else "UNSET"
        if (source == "CUSTOM") != (self.max_memory_tokens is not None):
            raise ValueError("memory source and explicit budget conflict")
        if self.max_memory_tokens is not None and not self.long_term_memory:
            raise ValueError("memory budget requires long_term_memory")
        self.max_memory_tokens_source = source
        return self


class ContextPolicyRequest(BaseModel):
    """An explicit request for the versioned adaptive input policy."""

    model_config = ConfigDict(extra="forbid")

    policy_version: Literal["adaptive_context_v1"]
    preset: Literal["GENERAL_64K", "STANDARD_32K", "LONG_DOCUMENT_128K"] = "GENERAL_64K"
    workspace_input_limit: int | None = Field(default=None, ge=1, strict=True)
    workspace_input_limit_source: Literal["UNSET", "DEFAULT", "CUSTOM"] | None = None
    soft_trigger_percent: int = Field(default=70, ge=70, le=70, strict=True)
    hard_trigger_percent: int = Field(default=85, ge=85, le=85, strict=True)
    target_percent: int = Field(default=55, ge=55, le=55, strict=True)
    counter_id: Literal["approx_language_v1"] = "approx_language_v1"

    @model_validator(mode="after")
    def validate_policy_intent(self) -> ContextPolicyRequest:
        normalized = normalize_context_policy_request(self.model_dump(exclude_none=True))
        self.workspace_input_limit_source = normalized["workspace_input_limit_source"]
        return self


class SummaryPolicyRequest(BaseModel):
    """Explicit opt-in for deterministic, source-grounded thread summaries."""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    policy_version: Literal["thread_summary_policy_v1"] = "thread_summary_policy_v1"
    prompt_version: Literal["summary_candidate_zh_v1"] = "summary_candidate_zh_v1"
    validator_version: Literal["summary_mechanical_v1"] = "summary_mechanical_v1"
    tokenizer_id: Literal["approx_language_v1"] = "approx_language_v1"
    tokenizer_version: int = Field(default=1, ge=1, le=1, strict=True)
    soft_trigger_percent: int = Field(default=70, ge=70, le=70, strict=True)
    hard_trigger_percent: int = Field(default=85, ge=85, le=85, strict=True)
    target_percent: int = Field(default=55, ge=55, le=55, strict=True)

    @model_validator(mode="after")
    def validate_explicit_opt_in(self) -> SummaryPolicyRequest:
        if self.enabled is not True:
            raise ValueError("summary_policy must be explicitly enabled")
        return self


class RuntimeConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int | None = Field(default=None, ge=1, le=MAX_RUNTIME_LIMITS["max_steps"])
    max_tool_calls: int | None = Field(default=None, ge=1, le=MAX_RUNTIME_LIMITS["max_tool_calls"])
    max_identical_calls: int | None = Field(
        default=None, ge=1, le=MAX_RUNTIME_LIMITS["max_identical_calls"]
    )
    max_parallel_reads: int | None = Field(
        default=None, ge=1, le=MAX_RUNTIME_LIMITS["max_parallel_reads"]
    )
    # Per-run spend ceiling in micro-USD (1_000_000 == USD 1.00). ``None``
    # leaves the run uncapped, which is what every agent published before this
    # field existed has.
    max_cost_micro_usd: int | None = Field(default=None, ge=1, le=MAX_RUN_COST_LIMIT_MICRO_USD)
    context_budget: ContextBudgetRequest | None = None
    context_budget_sources: (
        dict[
            Literal["reserved_output_tokens", "max_retrieval_tokens", "max_tool_result_tokens"],
            Literal["UNSET", "DEFAULT", "CUSTOM"],
        ]
        | None
    ) = None
    memory: MemoryConfigRequest | None = None
    context_policy: ContextPolicyRequest | None = None
    history_policy: Literal["deterministic_history_v1"] | None = None
    summary_policy: SummaryPolicyRequest | None = None

    @model_validator(mode="after")
    def validate_summary_prerequisites(self) -> RuntimeConfigRequest:
        if self.summary_policy is not None and (
            self.context_policy is None or self.history_policy != "deterministic_history_v1"
        ):
            raise ValueError("summary_policy requires context_policy and deterministic_history_v1")
        return self


class WikiBindingRequest(BaseModel):
    """Draft-level Wiki binding.

    ``null`` (a cleared binding) publishes byte-identically to a version
    published before Wiki binding existed. ``PINNED`` names one published
    compilation; ``LATEST`` resolves once at publish time.
    """

    model_config = ConfigDict(extra="forbid")

    knowledge_base_id: UUID
    binding_mode: Literal["PINNED", "LATEST"]
    wiki_publication_id: UUID | None = None


class WikiBindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    workspace_id: UUID
    agent_id: UUID
    knowledge_base_id: UUID
    binding_mode: Literal["PINNED", "LATEST"]
    wiki_publication_id: UUID | None
    publication_hash: str | None


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


class AgentPreflightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["READY"]
    agent_id: UUID
    workspace_id: UUID
    draft_updated_at: datetime
    spec_schema_version: int = Field(ge=1)
    resolved_spec_hash: str = Field(min_length=64, max_length=64)
    resolved_spec: dict[str, Any]


class AgentPublishResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_version_id: UUID
    agent_id: UUID
    version_number: int
    resolved_spec_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime


class AgentMemoryResponse(BaseModel):
    """What an agent remembers, as a human is allowed to see it.

    ``provenance`` is included on purpose: the first question anyone asks about
    a memory is "where did this come from", and answering it from the run and
    thread ids is the difference between a feature people trust and one they
    turn off. Nothing here is a secret -- the content is a sentence the agent
    was told, not a credential.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    agent_id: UUID
    thread_id: UUID | None
    source_run_id: UUID | None
    content: str
    kind: Literal["FACT", "PREFERENCE", "DECISION", "CONSTRAINT"]
    status: Literal["ACTIVE", "SUPERSEDED", "INVALIDATED"]
    superseded_by_id: UUID | None
    salience: int
    provenance: dict[str, Any]
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentMemoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AgentMemoryResponse]
    total: int


class AgentDeriveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_version_id: UUID
    source_version_number: int
    source_resolved_spec_hash: str
    values: dict[str, Any]
