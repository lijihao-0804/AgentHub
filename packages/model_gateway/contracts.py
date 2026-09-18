"""Provider-neutral contracts for the AgentHub model gateway."""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from packages.core.execution_context.models import WorkspaceExecutionContext

MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """A provider-neutral chat message."""

    role: MessageRole
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple["ModelToolCall", ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("tool_call_id is only valid for tool messages")
        calls = tuple(self.tool_calls)
        if any(not isinstance(call, ModelToolCall) for call in calls):
            raise TypeError("tool_calls must contain ModelToolCall values")
        object.__setattr__(self, "tool_calls", calls)


@dataclass(frozen=True, slots=True)
class ModelToolDefinition:
    """A provider-neutral tool declaration."""

    name: str
    parameters: Mapping[str, Any]
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    """A normalized tool call; provider IDs are protocol metadata only."""

    name: str
    arguments: Mapping[str, Any]
    provider_tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class StructuredOutputSchema:
    """Optional JSON schema requested from a model."""

    name: str
    json_schema: Mapping[str, Any]
    strict: bool = True


@dataclass(frozen=True, slots=True)
class CapabilityRequirements:
    """Capabilities that a selected profile and its fallbacks must provide."""

    required: frozenset[str] = field(default_factory=frozenset)
    max_context_tokens: int | None = None

    def __post_init__(self) -> None:
        normalized = frozenset(self.required)
        if any(not capability or not isinstance(capability, str) for capability in normalized):
            raise ValueError("required capabilities must be non-empty strings")
        if self.max_context_tokens is not None and self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        object.__setattr__(self, "required", normalized)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """A bounded request retry policy; resolved runtime snapshots own its future use."""

    max_attempts: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Provider-neutral model input. Provider selection is server-side."""

    messages: tuple[ModelMessage, ...]
    tools: tuple[ModelToolDefinition, ...] = field(default_factory=tuple)
    response_schema: StructuredOutputSchema | None = None
    required_capabilities: CapabilityRequirements = field(default_factory=CapabilityRequirements)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        messages = tuple(self.messages)
        tools = tuple(self.tools)
        if any(not isinstance(message, ModelMessage) for message in messages):
            raise TypeError("messages must contain ModelMessage values")
        if any(not isinstance(tool, ModelToolDefinition) for tool in tools):
            raise TypeError("tools must contain ModelToolDefinition values")
        if not isinstance(self.required_capabilities, CapabilityRequirements):
            raise TypeError("required_capabilities must be CapabilityRequirements")
        if not isinstance(self.retry_policy, RetryPolicy):
            raise TypeError("retry_policy must be RetryPolicy")
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "tools", tools)


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int | None = None

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens, self.total_tokens)
        if any(value < 0 for value in values):
            raise ValueError("token counts must be non-negative")
        if self.cached_tokens is not None and self.cached_tokens < 0:
            raise ValueError("cached_tokens must be non-negative")


@dataclass(frozen=True, slots=True)
class CostEstimate:
    amount: Decimal
    currency: str
    is_estimate: bool = True

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("cost amount must be non-negative")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("currency must be a three-letter code")
        object.__setattr__(self, "currency", self.currency.upper())


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    provider: str
    model: str
    tool_calls: tuple[ModelToolCall, ...] = field(default_factory=tuple)
    finish_reason: str | None = None
    usage: ModelUsage | None = None
    cost_estimate: CostEstimate | None = None

    def __post_init__(self) -> None:
        calls = tuple(self.tool_calls)
        if any(not isinstance(call, ModelToolCall) for call in calls):
            raise TypeError("tool_calls must contain ModelToolCall values")
        object.__setattr__(self, "tool_calls", calls)


class ModelStreamEventType(StrEnum):
    MESSAGE_DELTA = "message.delta"
    TOOL_CALL_DELTA = "tool_call.delta"
    USAGE = "usage"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ModelToolCallDelta:
    index: int
    name: str | None = None
    arguments_delta: str = ""
    provider_tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("tool call index must be non-negative")


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    """Typed stream event with a single lifecycle event type."""

    event_type: ModelStreamEventType
    message_delta: str | None = None
    tool_call_delta: ModelToolCallDelta | None = None
    usage: ModelUsage | None = None
    response: ModelResponse | None = None

    def __post_init__(self) -> None:
        payloads = (
            self.message_delta is not None,
            self.tool_call_delta is not None,
            self.usage is not None,
            self.response is not None,
        )
        if sum(payloads) != 1:
            raise ValueError("stream events must contain exactly one payload")
        expected = {
            ModelStreamEventType.MESSAGE_DELTA: self.message_delta is not None,
            ModelStreamEventType.TOOL_CALL_DELTA: self.tool_call_delta is not None,
            ModelStreamEventType.USAGE: self.usage is not None,
            ModelStreamEventType.COMPLETED: self.response is not None,
        }
        if not expected[self.event_type]:
            raise ValueError("stream event payload does not match event_type")


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    tool_calling: bool = False
    streaming: bool = False
    structured_output: bool = False
    vision: bool = False
    max_context_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_context_tokens is not None and self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")


@dataclass(frozen=True, slots=True)
class ResolvedModelExecutionProfile:
    """The non-secret model identity frozen into an immutable AgentVersion."""

    id: UUID
    workspace_id: UUID
    provider_credential_id: UUID
    provider: str
    model: str
    temperature: Decimal
    max_tokens: int
    timeout_seconds: Decimal
    capabilities: ModelCapabilities


@dataclass(frozen=True, slots=True)
class ResolvedModelExecutionPlan:
    """A published model chain plus the retry policy frozen with that chain."""

    primary: ResolvedModelExecutionProfile
    fallbacks: tuple[ResolvedModelExecutionProfile, ...] = field(default_factory=tuple)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)

    @property
    def chain(self) -> tuple[ResolvedModelExecutionProfile, ...]:
        return (self.primary, *self.fallbacks)


class ModelHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ModelHealthResult:
    status: ModelHealthStatus
    failure_code: str | None = None


class ModelGateway(Protocol):
    async def generate(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
        request: ModelRequest,
    ) -> ModelResponse: ...

    async def generate_resolved(
        self,
        context: WorkspaceExecutionContext,
        plan: ResolvedModelExecutionPlan,
        request: ModelRequest,
    ) -> ModelResponse: ...

    def stream(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]: ...

    def stream_resolved(
        self,
        context: WorkspaceExecutionContext,
        plan: ResolvedModelExecutionPlan,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]: ...

    async def health(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
    ) -> ModelHealthResult: ...

    async def capabilities(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
    ) -> ModelCapabilities: ...


__all__ = [
    "CapabilityRequirements",
    "CostEstimate",
    "MessageRole",
    "ModelCapabilities",
    "ModelGateway",
    "ModelHealthResult",
    "ModelHealthStatus",
    "ModelMessage",
    "ModelRequest",
    "ModelResponse",
    "ResolvedModelExecutionPlan",
    "ResolvedModelExecutionProfile",
    "ModelStreamEvent",
    "ModelStreamEventType",
    "ModelToolCall",
    "ModelToolCallDelta",
    "ModelToolDefinition",
    "ModelUsage",
    "RetryPolicy",
    "StructuredOutputSchema",
]
