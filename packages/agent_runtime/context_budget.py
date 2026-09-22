"""Provider-neutral context admission for the Agent runtime.

The policy in this module deliberately operates on already frozen model
profiles and provider-neutral model contracts.  It does not know how a
provider tokenizes text, how tools are implemented, or how conversation
messages were produced.  Callers therefore have to provide the category and
exchange-group metadata explicitly.

The default estimator is intentionally conservative and offline.  It counts
each UTF-8 byte as one token unit; the policy accepts an injected estimator so
tests and future adapters can use a provider-specific estimate without
changing admission semantics.
"""

from __future__ import annotations

import json
import math
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, cast

from packages.agent_runtime.frozen import FrozenAgentSpec
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelToolDefinition,
    ResolvedModelExecutionPlan,
)


class ContextCategory(StrEnum):
    """The explicit semantic category of one context item."""

    RUNTIME_POLICY = "RUNTIME_POLICY"
    SYSTEM_PROMPT = "SYSTEM_PROMPT"
    CURRENT_USER_TASK = "CURRENT_USER_TASK"
    TOOL_DEFINITIONS = "TOOL_DEFINITIONS"
    RAG_EVIDENCE = "RAG_EVIDENCE"
    TOOL_RESULT = "TOOL_RESULT"
    CONVERSATION = "CONVERSATION"
    # Long-term memory injected from an earlier conversation.  Evidence, not
    # instruction: a preference recorded three months ago must never be able to
    # push the question being asked now out of the window, which is exactly
    # what putting it in the mandatory tier would let it do.
    MEMORY = "MEMORY"


_MANDATORY_CATEGORIES = frozenset(
    {
        ContextCategory.RUNTIME_POLICY,
        ContextCategory.SYSTEM_PROMPT,
        ContextCategory.CURRENT_USER_TASK,
        ContextCategory.TOOL_DEFINITIONS,
    }
)
_PROJECTABLE_CATEGORIES = frozenset(
    {ContextCategory.RAG_EVIDENCE, ContextCategory.TOOL_RESULT, ContextCategory.MEMORY}
)
# Content that entered the window from outside the published agent version.
# Tool output is untrusted because a remote system produced it; memory is
# untrusted because a model wrote it.  Neither may promote itself by putting a
# friendlier ``trust`` value in its own payload.
_UNTRUSTED_CATEGORIES = frozenset(
    {ContextCategory.TOOL_RESULT, ContextCategory.MEMORY}
)
_ALL_CATEGORIES = frozenset(ContextCategory)
_PROJECTION_UNAVAILABLE = object()


@dataclass(frozen=True, slots=True)
class ContextBudgetConfig:
    """Runtime context budget knobs stored in the published runtime policy."""

    reserved_output_tokens: int = 2_000
    max_retrieval_tokens: int = 5_000
    max_tool_result_tokens: int = 4_000
    # None preserves the historical shared memory+retrieval evidence pool.
    # Published versions created with long-term memory enabled carry this
    # server-owned independent cap explicitly.
    max_memory_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "reserved_output_tokens",
            "max_retrieval_tokens",
            "max_tool_result_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_memory_tokens is not None and (
            isinstance(self.max_memory_tokens, bool)
            or not isinstance(self.max_memory_tokens, int)
            or self.max_memory_tokens < 0
        ):
            raise ValueError("max_memory_tokens must be a non-negative integer or None")


class TokenEstimator(Protocol):
    """The small provider-neutral surface required by context admission."""

    def estimate(self, text: str) -> int:
        """Return a deterministic non-negative token estimate for ``text``."""


@dataclass(frozen=True, slots=True)
class Utf8ByteTokenEstimator:
    """Deterministic offline conservative UTF-8-byte token-unit estimator.

    This is intentionally not an exact provider tokenizer count.  Treating
    each UTF-8 byte as one token unit avoids underestimating non-ASCII text
    without downloading or depending on a provider-specific tokenizer.
    """

    def estimate(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("TokenEstimator.estimate expects text")
        return len(text.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ContextMessage:
    """A model message with the metadata admission must not infer.

    ``exchange_group`` is an opaque caller-owned key.  Messages in one group
    are admitted or dropped together.  In particular, an assistant tool-call
    message and its tool observations must be put in the same group.
    """

    message: ModelMessage
    category: ContextCategory
    exchange_group: Hashable | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, ModelMessage):
            raise TypeError("message must be a ModelMessage")
        object.__setattr__(self, "category", _coerce_category(self.category))
        if self.exchange_group is not None:
            try:
                hash(self.exchange_group)
            except TypeError:
                raise TypeError("exchange_group must be hashable") from None

    @property
    def role(self) -> str:
        return self.message.role

    @property
    def content(self) -> str | None:
        return self.message.content


# These names make the explicit metadata contract discoverable without
# forcing callers to choose between common terminology.
type CategorizedMessage = ContextMessage
type ContextItem = ContextMessage


@dataclass(frozen=True, slots=True)
class ContextBudgetUsage:
    """Numeric admission telemetry; message text is intentionally absent."""

    context_limit: int
    reserved_output: int
    available_input: int
    estimated_input_before: int
    estimated_input_after: int
    truncated: bool
    dropped_exchange_count: int
    estimated_by_category: Mapping[ContextCategory, int] = field(default_factory=dict)
    retained_by_category: Mapping[ContextCategory, int] = field(default_factory=dict)
    truncated_by_category: Mapping[ContextCategory, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        numeric = (
            self.context_limit,
            self.reserved_output,
            self.available_input,
            self.estimated_input_before,
            self.estimated_input_after,
            self.dropped_exchange_count,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in numeric
        ):
            raise ValueError("context usage counts must be non-negative integers")
        if self.reserved_output > self.context_limit:
            raise ValueError("reserved output cannot exceed context limit")
        if self.available_input != self.context_limit - self.reserved_output:
            raise ValueError("available input must equal context limit minus reserved output")
        if self.estimated_input_after > self.available_input:
            raise ValueError("admitted input cannot exceed available input")
        for name in (
            "estimated_by_category",
            "retained_by_category",
            "truncated_by_category",
        ):
            value = getattr(self, name)
            normalized: dict[ContextCategory, int] = {}
            for category, count in value.items():
                category = _coerce_category(category)
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError(f"{name} values must be non-negative integers")
                normalized[category] = count
            object.__setattr__(self, name, MappingProxyType(normalized))

    @property
    def estimated_input_by_category(self) -> Mapping[ContextCategory, int]:
        return self.estimated_by_category

    @property
    def retained_input_by_category(self) -> Mapping[ContextCategory, int]:
        return self.retained_by_category

    @property
    def truncated_input_by_category(self) -> Mapping[ContextCategory, int]:
        return self.truncated_by_category

    @property
    def estimated_tokens_by_category(self) -> Mapping[ContextCategory, int]:
        return self.estimated_by_category

    @property
    def retained_tokens_by_category(self) -> Mapping[ContextCategory, int]:
        return self.retained_by_category

    @property
    def truncated_tokens_by_category(self) -> Mapping[ContextCategory, int]:
        return self.truncated_by_category


@dataclass(frozen=True, slots=True)
class ContextAdmissionResult:
    """The provider-neutral model input after budget admission."""

    messages: tuple[ModelMessage, ...]
    tool_definitions: tuple[ModelToolDefinition, ...]
    usage: ContextBudgetUsage

    def __post_init__(self) -> None:
        messages = tuple(self.messages)
        tools = tuple(self.tool_definitions)
        if any(not isinstance(message, ModelMessage) for message in messages):
            raise TypeError("messages must contain ModelMessage values")
        if any(not isinstance(tool, ModelToolDefinition) for tool in tools):
            raise TypeError("tool_definitions must contain ModelToolDefinition values")
        if not isinstance(self.usage, ContextBudgetUsage):
            raise TypeError("usage must be ContextBudgetUsage")
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "tool_definitions", tools)

    @property
    def admitted_messages(self) -> tuple[ModelMessage, ...]:
        return self.messages

    @property
    def admitted_tool_definitions(self) -> tuple[ModelToolDefinition, ...]:
        return self.tool_definitions


@dataclass(frozen=True, slots=True)
class _NormalizedMessage:
    item: ContextMessage
    index: int
    raw_tokens: int
    admitted_message: ModelMessage | None = None
    admitted_tokens: int = 0
    was_projected: bool = False

    @property
    def category(self) -> ContextCategory:
        return self.item.category

    @property
    def group(self) -> Hashable:
        if self.item.exchange_group is not None:
            return self.item.exchange_group
        # A missing group is intentionally not inferred from roles, text, or
        # tool names.  Each ungrouped message is its own atomic exchange.
        return ("__ungrouped_context_message__", self.index)


@dataclass(frozen=True, slots=True)
class _ToolEstimate:
    tool_name: str
    total: int
    name: int
    description: int
    schema: int


class ContextBudgetPolicy:
    """Admit explicitly categorized context against a frozen model chain."""

    def __init__(
        self,
        frozen_spec: FrozenAgentSpec | ResolvedModelExecutionPlan | Iterable[Any] | None = None,
        config: ContextBudgetConfig | None = None,
        *,
        model_plan: ResolvedModelExecutionPlan | None = None,
        estimator: TokenEstimator | None = None,
        profiles: Iterable[Any] | None = None,
    ) -> None:
        supplied = [value for value in (frozen_spec, model_plan, profiles) if value is not None]
        if len(supplied) != 1:
            raise TypeError("provide exactly one of frozen_spec, model_plan, or profiles")
        self._profile_source = supplied[0]
        self.config = config or ContextBudgetConfig()
        self.estimator: TokenEstimator = estimator or Utf8ByteTokenEstimator()
        if not hasattr(self.estimator, "estimate"):
            raise TypeError("estimator must implement estimate(text)")

    @classmethod
    def from_frozen_spec(
        cls,
        frozen_spec: FrozenAgentSpec,
        config: ContextBudgetConfig | None = None,
        *,
        estimator: TokenEstimator | None = None,
    ) -> ContextBudgetPolicy:
        return cls(frozen_spec, config, estimator=estimator)

    @property
    def context_limit(self) -> int:
        return self._effective_context_limit()

    @property
    def effective_context_limit(self) -> int:
        return self.context_limit

    @property
    def reserved_output_tokens(self) -> int:
        return self._effective_reserved_output()

    @property
    def effective_reserved_output_tokens(self) -> int:
        return self.reserved_output_tokens

    def admit(
        self,
        messages: Sequence[ModelMessage | ContextMessage] = (),
        categories: Sequence[ContextCategory] | Mapping[int, ContextCategory] | None = None,
        exchange_groups: Sequence[Hashable | None]
        | Mapping[int, Hashable | None]
        | None = None,
        tool_definitions: Sequence[ModelToolDefinition] = (),
    ) -> ContextAdmissionResult:
        """Return the admitted messages and tools.

        Raw ``ModelMessage`` values require parallel ``categories`` and may
        optionally receive parallel ``exchange_groups``.  Alternatively,
        callers can pass ``ContextMessage`` values, which carry both pieces of
        metadata inline.  No category is inferred from message text, roles, or
        tool names.
        """

        normalized = self._normalize_messages(messages, categories, exchange_groups)
        tools = tuple(tool_definitions)
        if any(not isinstance(tool, ModelToolDefinition) for tool in tools):
            raise TypeError("tool_definitions must contain ModelToolDefinition values")
        return self._admit_normalized(normalized, tools)

    def admit_context(
        self,
        messages: Sequence[ModelMessage | ContextMessage] = (),
        categories: Sequence[ContextCategory] | Mapping[int, ContextCategory] | None = None,
        exchange_groups: Sequence[Hashable | None]
        | Mapping[int, Hashable | None]
        | None = None,
        tool_definitions: Sequence[ModelToolDefinition] = (),
    ) -> ContextAdmissionResult:
        """Descriptive alias for :meth:`admit` used by runtime integrations."""

        return self.admit(messages, categories, exchange_groups, tool_definitions)

    def _normalize_messages(
        self,
        messages: Sequence[ModelMessage | ContextMessage],
        categories: Sequence[ContextCategory] | Mapping[int, ContextCategory] | None,
        exchange_groups: Sequence[Hashable | None]
        | Mapping[int, Hashable | None]
        | None,
    ) -> tuple[ContextMessage, ...]:
        if isinstance(messages, (str, bytes)):
            raise TypeError("messages must be a sequence of ModelMessage values")
        values = tuple(messages)
        if categories is not None and len(values) != _metadata_length(categories):
            raise ValueError("categories must contain one value per message")
        if exchange_groups is not None and len(values) != _metadata_length(exchange_groups):
            raise ValueError("exchange_groups must contain one value per message")

        normalized: list[ContextMessage] = []
        for index, value in enumerate(values):
            if isinstance(value, ContextMessage):
                category = value.category
                group = value.exchange_group
                if categories is not None and _metadata_at(categories, index) != category:
                    raise ValueError("inline category conflicts with categories")
                if exchange_groups is not None and _metadata_at(exchange_groups, index) != group:
                    raise ValueError("inline exchange_group conflicts with exchange_groups")
                normalized.append(value)
                continue
            if not isinstance(value, ModelMessage):
                raise TypeError("messages must contain ModelMessage or ContextMessage values")
            if categories is None:
                raise ValueError(
                    "categories are required for raw messages; context categories must be explicit"
                )
            category = _coerce_category(_metadata_at(categories, index))
            group = None if exchange_groups is None else _metadata_at(exchange_groups, index)
            if group is not None:
                try:
                    hash(group)
                except TypeError:
                    raise TypeError("exchange_group must be hashable") from None
            normalized.append(
                ContextMessage(message=value, category=category, exchange_group=group)
            )
        _validate_atomic_tool_groups(normalized)
        return tuple(normalized)

    def _admit_normalized(
        self,
        messages: tuple[ContextMessage, ...],
        tools: tuple[ModelToolDefinition, ...],
    ) -> ContextAdmissionResult:
        context_limit = self._effective_context_limit()
        reserved_output = self._effective_reserved_output()
        if reserved_output >= context_limit:
            raise _budget_exceeded(
                "Reserved output tokens must be smaller than the effective context limit."
            )
        available_input = context_limit - reserved_output

        tool_estimates = tuple(self._estimate_tool(tool) for tool in tools)
        raw_items = tuple(
            _NormalizedMessage(
                item=item,
                index=index,
                raw_tokens=self._estimate_message(item.message),
            )
            for index, item in enumerate(messages)
        )

        estimated_by_category = {category: 0 for category in ContextCategory}
        for item in raw_items:
            estimated_by_category[item.category] += item.raw_tokens
        estimated_by_category[ContextCategory.TOOL_DEFINITIONS] += sum(
            estimate.total for estimate in tool_estimates
        )
        estimated_input_before = sum(estimated_by_category.values())

        mandatory_items = tuple(
            item for item in raw_items if item.category in _MANDATORY_CATEGORIES
        )
        mandatory_message_tokens = sum(item.raw_tokens for item in mandatory_items)
        mandatory_tool_tokens = sum(estimate.total for estimate in tool_estimates)
        mandatory_total = mandatory_message_tokens + mandatory_tool_tokens
        if mandatory_total > available_input:
            raise _mandatory_overflow(
                mandatory_total=mandatory_total,
                available_input=available_input,
                tool_estimates=tool_estimates,
            )

        projected = self._project_optional_items(raw_items)
        selected, dropped_exchange_count = self._select_items(
            projected,
            mandatory_total=mandatory_total,
            available_input=available_input,
        )
        selected_indices = {item.index for item in selected}

        retained_by_category = {category: 0 for category in ContextCategory}
        truncated_by_category = {category: 0 for category in ContextCategory}
        for item in raw_items:
            if item.index in selected_indices:
                retained = next(
                    selected_item.admitted_tokens
                    for selected_item in selected
                    if selected_item.index == item.index
                )
                retained_by_category[item.category] += retained
                truncated_by_category[item.category] += max(item.raw_tokens - retained, 0)
            else:
                truncated_by_category[item.category] += item.raw_tokens
        retained_by_category[ContextCategory.TOOL_DEFINITIONS] = mandatory_tool_tokens + sum(
            item.admitted_tokens
            for item in selected
            if item.category is ContextCategory.TOOL_DEFINITIONS
        )
        truncated_by_category[ContextCategory.TOOL_DEFINITIONS] = 0

        admitted_messages = tuple(
            item.admitted_message
            for item in sorted(selected, key=lambda item: item.index)
            if item.admitted_message is not None
        )
        estimated_input_after = mandatory_tool_tokens + sum(
            item.admitted_tokens for item in selected
        )
        if estimated_input_after > available_input:
            raise _budget_exceeded(
                "Context admission exceeded the available input budget after projection."
            )

        truncated = any(value > 0 for value in truncated_by_category.values())
        usage = ContextBudgetUsage(
            context_limit=context_limit,
            reserved_output=reserved_output,
            available_input=available_input,
            estimated_input_before=estimated_input_before,
            estimated_input_after=estimated_input_after,
            truncated=truncated,
            dropped_exchange_count=dropped_exchange_count,
            estimated_by_category=estimated_by_category,
            retained_by_category=retained_by_category,
            truncated_by_category=truncated_by_category,
        )
        return ContextAdmissionResult(
            messages=admitted_messages,
            tool_definitions=tools,
            usage=usage,
        )

    def _project_optional_items(
        self,
        items: tuple[_NormalizedMessage, ...],
    ) -> tuple[_NormalizedMessage, ...]:
        projected: list[_NormalizedMessage] = []
        # Old specs keep the shared evidence pool for hash and behaviour
        # compatibility. New memory-enabled specs carry an independent cap so
        # a large memory set cannot starve current retrieval evidence.
        if self.config.max_memory_tokens is None:
            pools = {
                "evidence": self.config.max_retrieval_tokens,
                "tool_result": self.config.max_tool_result_tokens,
            }
            allocations = (
                (ContextCategory.MEMORY, "evidence"),
                (ContextCategory.RAG_EVIDENCE, "evidence"),
                (ContextCategory.TOOL_RESULT, "tool_result"),
            )
        else:
            pools = {
                "memory": self.config.max_memory_tokens,
                "evidence": self.config.max_retrieval_tokens,
                "tool_result": self.config.max_tool_result_tokens,
            }
            allocations = (
                (ContextCategory.MEMORY, "memory"),
                (ContextCategory.RAG_EVIDENCE, "evidence"),
                (ContextCategory.TOOL_RESULT, "tool_result"),
            )
        for category, pool in allocations:
            remaining = pools[pool]
            category_items = [item for item in items if item.category is category]
            if category is ContextCategory.TOOL_RESULT:
                # Newer observations belong to the newest exchange more often
                # than older observations do.  Allocate this independent cap
                # newest-first so an old result cannot starve the current turn.
                category_items.reverse()
            for item in category_items:
                message = item.item.message
                admitted_message, admitted_tokens = self._project_message(
                    item.item, remaining, category
                )
                was_projected = admitted_message != message
                if admitted_message is None or admitted_tokens > remaining:
                    admitted_message = None
                    admitted_tokens = 0
                    was_projected = True
                if admitted_message is not None:
                    remaining -= admitted_tokens
                projected.append(
                    replace(
                        item,
                        admitted_message=admitted_message,
                        admitted_tokens=admitted_tokens,
                        was_projected=was_projected,
                    )
                )
            pools[pool] = remaining

        projected_by_index = {item.index: item for item in projected}
        result: list[_NormalizedMessage] = []
        for item in items:
            if item.category in _MANDATORY_CATEGORIES:
                result.append(
                    replace(
                        item,
                        admitted_message=item.item.message,
                        admitted_tokens=item.raw_tokens,
                    )
                )
            elif item.category in _PROJECTABLE_CATEGORIES:
                result.append(projected_by_index[item.index])
            else:
                result.append(
                    replace(
                        item,
                        admitted_message=item.item.message,
                        admitted_tokens=item.raw_tokens,
                    )
                )
        return tuple(result)

    def _select_items(
        self,
        items: tuple[_NormalizedMessage, ...],
        *,
        mandatory_total: int,
        available_input: int,
    ) -> tuple[tuple[_NormalizedMessage, ...], int]:
        budget = available_input - mandatory_total
        groups: dict[Hashable, list[_NormalizedMessage]] = {}
        first_index: dict[Hashable, int] = {}
        for item in items:
            if item.category in _MANDATORY_CATEGORIES:
                continue
            groups.setdefault(item.group, []).append(item)
            first_index.setdefault(item.group, item.index)

        conversation_groups = [
            group
            for group, group_items in groups.items()
            if any(item.category is ContextCategory.CONVERSATION for item in group_items)
        ]
        newest_group = max(conversation_groups, key=lambda group: first_index[group], default=None)

        selected_groups: set[Hashable] = set()

        # The newest conversation exchange is the one user-visible continuity
        # guarantee.  It is selected first and can never be displaced by old
        # evidence or old conversation groups.
        if newest_group is not None:
            newest_items = groups[newest_group]
            if any(item.admitted_message is None for item in newest_items):
                raise _budget_exceeded(
                    "The newest conversation exchange could not be admitted atomically."
                )
            newest_tokens = sum(item.admitted_tokens for item in newest_items)
            if newest_tokens > budget:
                raise _budget_exceeded(
                    "The newest conversation exchange cannot fit in the available input budget."
                )

        # Start with every eligible conversation exchange, then remove the
        # oldest groups until the remaining budget is satisfied.  This keeps
        # a newest suffix instead of accidentally retaining an old group while
        # dropping a newer, larger middle group.
        eligible_conversation_groups = [
            group
            for group in conversation_groups
            if all(item.admitted_message is not None for item in groups[group])
        ]
        selected_conversation_groups = set(eligible_conversation_groups)
        conversation_tokens = sum(
            sum(item.admitted_tokens for item in groups[group])
            for group in selected_conversation_groups
        )
        while conversation_tokens > budget and selected_conversation_groups:
            oldest = min(selected_conversation_groups, key=lambda key: first_index[key])
            selected_conversation_groups.remove(oldest)
            conversation_tokens -= sum(
                item.admitted_tokens for item in groups[oldest]
            )
        selected_groups.update(selected_conversation_groups)
        remaining = budget - conversation_tokens

        # Standalone evidence/results use their original order.  They are
        # already bounded independently by _project_optional_items.
        for group in sorted(groups, key=lambda key: first_index[key]):
            if group in selected_groups or group in conversation_groups:
                continue
            if any(item.admitted_message is None for item in groups[group]):
                continue
            group_tokens = sum(item.admitted_tokens for item in groups[group])
            if group_tokens <= remaining:
                selected_groups.add(group)
                remaining -= group_tokens

        selected = tuple(
            item
            for item in items
            if item.category in _MANDATORY_CATEGORIES or item.group in selected_groups
        )
        dropped_exchange_count = sum(
            1
            for group in conversation_groups
            if group not in selected_groups
        )
        return selected, dropped_exchange_count

    def _project_message(
        self,
        item: ContextMessage,
        budget: int,
        category: ContextCategory,
    ) -> tuple[ModelMessage | None, int]:
        if budget <= 0:
            return None, 0
        raw_content = item.message.content
        parsed: Any
        if raw_content is None:
            parsed = None
        else:
            try:
                parsed = json.loads(raw_content)
            except (TypeError, ValueError):
                parsed = raw_content
        projected, was_truncated = _project_json_value(
            parsed,
            budget=budget,
            category=category,
            estimator=self.estimator,
        )
        if projected is _PROJECTION_UNAVAILABLE:
            return None, 0
        content = _json_dumps(projected)
        if was_truncated:
            # _project_json_value always returns JSON-safe data with a marker,
            # but this guard protects custom estimators and future changes.
            try:
                json.loads(content)
            except ValueError:
                return None, 0
        message = replace(item.message, content=content)
        estimated = self._estimate_message(message)
        if estimated > budget:
            # A final legal marker is preferable to a sliced JSON string.  It
            # may still be too large for an intentionally pathological custom
            # estimator, in which case the item is omitted safely.
            marker: dict[str, Any] = {"truncated": True}
            if category in _UNTRUSTED_CATEGORIES:
                marker["trust"] = "UNTRUSTED"
            marker_message = replace(item.message, content=_json_dumps(marker))
            marker_estimated = self._estimate_message(marker_message)
            if marker_estimated > budget:
                return None, 0
            return marker_message, marker_estimated
        return message, estimated

    def _estimate_message(self, message: ModelMessage) -> int:
        total = self._estimate_text(message.content or "")
        if message.name:
            total += self._estimate_text(message.name)
        if message.tool_call_id:
            total += self._estimate_text(message.tool_call_id)
        for call in message.tool_calls:
            total += self._estimate_text(call.name)
            total += self._estimate_text(_json_dumps(dict(call.arguments)))
            if call.provider_tool_call_id:
                total += self._estimate_text(call.provider_tool_call_id)
        return total

    def _estimate_tool(self, tool: ModelToolDefinition) -> _ToolEstimate:
        name = self._estimate_text(tool.name)
        description = self._estimate_text(tool.description or "")
        schema = self._estimate_text(_json_dumps(dict(tool.parameters)))
        return _ToolEstimate(
            tool_name=tool.name,
            total=name + description + schema,
            name=name,
            description=description,
            schema=schema,
        )

    def _estimate_text(self, text: str) -> int:
        value = self.estimator.estimate(text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("TokenEstimator.estimate must return a non-negative integer")
        return value

    def _profiles(self) -> tuple[Any, ...]:
        source = self._profile_source
        if isinstance(source, FrozenAgentSpec):
            return tuple(source.model_plan.chain)
        if isinstance(source, ResolvedModelExecutionPlan):
            return tuple(source.chain)
        if hasattr(source, "model_plan"):
            plan = source.model_plan
            if hasattr(plan, "chain"):
                return tuple(plan.chain)
        if hasattr(source, "chain") and not isinstance(source, (str, bytes, Mapping)):
            return tuple(source.chain)
        if isinstance(source, Mapping):
            return tuple(source.values())
        try:
            return tuple(cast(Iterable[Any], source))
        except TypeError:
            return (source,)

    def _effective_context_limit(self) -> int:
        profiles = self._profiles()
        if not profiles:
            raise _context_limit_unavailable("No frozen model profiles were provided.")
        limits: list[int] = []
        for profile in profiles:
            value = _profile_context_limit(profile)
            if value is None:
                raise _context_limit_unavailable(
                    "Every frozen primary and fallback model profile must declare "
                    "max_context_tokens."
                )
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise _context_limit_unavailable(
                    "Frozen model max_context_tokens must be a positive integer."
                )
            limits.append(value)
        return min(limits)

    def _effective_reserved_output(self) -> int:
        maximum = self.config.reserved_output_tokens
        for profile in self._profiles():
            value = getattr(profile, "max_tokens", None)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _budget_exceeded("Frozen model max_tokens must be a non-negative integer.")
            maximum = max(maximum, value)
        if maximum >= self._effective_context_limit():
            raise _budget_exceeded(
                "Reserved output tokens must be smaller than the effective context limit."
            )
        return maximum


def admit_context(
    frozen_spec: FrozenAgentSpec | ResolvedModelExecutionPlan | Iterable[Any],
    messages: Sequence[ModelMessage | ContextMessage] = (),
    categories: Sequence[ContextCategory] | Mapping[int, ContextCategory] | None = None,
    exchange_groups: Sequence[Hashable | None]
    | Mapping[int, Hashable | None]
    | None = None,
    tool_definitions: Sequence[ModelToolDefinition] = (),
    *,
    config: ContextBudgetConfig | None = None,
    estimator: TokenEstimator | None = None,
) -> ContextAdmissionResult:
    """Functional entry point for runtime code that does not retain a policy."""

    return ContextBudgetPolicy(
        frozen_spec,
        config,
        estimator=estimator,
    ).admit(messages, categories, exchange_groups, tool_definitions)


def _coerce_category(value: ContextCategory | str) -> ContextCategory:
    if isinstance(value, ContextCategory):
        return value
    try:
        return ContextCategory(value)
    except (TypeError, ValueError):
        raise ValueError(f"unknown context category: {value!r}") from None


def _metadata_length(value: Sequence[Any] | Mapping[int, Any]) -> int:
    return len(value)


def _metadata_at(value: Sequence[Any] | Mapping[int, Any], index: int) -> Any:
    if isinstance(value, Mapping):
        if index not in value:
            raise ValueError(f"metadata is missing index {index}")
        return value[index]
    return value[index]


def _profile_context_limit(profile: Any) -> Any:
    capabilities = getattr(profile, "capabilities", None)
    if capabilities is not None:
        return getattr(capabilities, "max_context_tokens", None)
    return getattr(profile, "max_context_tokens", None)


def _context_limit_unavailable(message: str) -> AgentHubError:
    return AgentHubError("AGENT_CONTEXT_LIMIT_UNAVAILABLE", message, 422)


def _budget_exceeded(message: str) -> AgentHubError:
    return AgentHubError("AGENT_CONTEXT_BUDGET_EXCEEDED", message, 422)


def _mandatory_overflow(
    *,
    mandatory_total: int,
    available_input: int,
    tool_estimates: Sequence[_ToolEstimate],
) -> AgentHubError:
    details = [
        "Mandatory context exceeds the available input budget",
        f"required={mandatory_total}",
        f"available={available_input}",
        f"tool_count={len(tool_estimates)}",
    ]
    for index, estimate in enumerate(tool_estimates):
        details.append(
            f"tool[{index}] {estimate.tool_name!r} "
            f"name_tokens={estimate.name} description_tokens={estimate.description} "
            f"json_schema_tokens={estimate.schema}"
        )
    return _budget_exceeded("; ".join(details) + ".")


def _validate_atomic_tool_groups(messages: Sequence[ContextMessage]) -> None:
    groups: dict[Hashable, list[ContextMessage]] = {}
    for index, item in enumerate(messages):
        group = (
            item.exchange_group
            if item.exchange_group is not None
            else ("__ungrouped_context_message__", index)
        )
        groups.setdefault(group, []).append(item)
    for group_items in groups.values():
        tool_messages = [item for item in group_items if item.message.role == "tool"]
        if not tool_messages:
            continue
        has_assistant_tool_call = any(
            item.message.role == "assistant" and bool(item.message.tool_calls)
            for item in group_items
        )
        if not has_assistant_tool_call:
            raise ValueError(
                "tool observations must share an explicit exchange_group with an "
                "assistant tool call"
            )


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_estimate(value: Any, estimator: TokenEstimator) -> int:
    result = estimator.estimate(_json_dumps(value))
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ValueError("TokenEstimator.estimate must return a non-negative integer")
    return result


def _project_json_value(
    value: Any,
    *,
    budget: int,
    category: ContextCategory,
    estimator: TokenEstimator,
) -> tuple[Any, bool]:
    """Return a legal bounded JSON projection and whether it was truncated."""

    if budget <= 0:
        return _PROJECTION_UNAVAILABLE, True
    normalized = _json_safe(value)
    trust_fields: dict[str, Any] = {}
    if category in _UNTRUSTED_CATEGORIES:
        # Tool output and recalled memory are untrusted boundaries regardless
        # of labels supplied by the payload itself.  Never allow a payload to
        # promote itself.
        trust_fields["trust"] = "UNTRUSTED"
    if isinstance(normalized, Mapping) and category not in _UNTRUSTED_CATEGORIES:
        for key in ("trust", "data_trust"):
            if key in normalized:
                trust_fields[key] = normalized[key]
    if _json_estimate(normalized, estimator) <= budget and not trust_fields:
        return normalized, False
    if _json_estimate(normalized, estimator) <= budget:
        candidate = dict(normalized) if isinstance(normalized, Mapping) else normalized
        if isinstance(candidate, dict):
            for key, trust in trust_fields.items():
                # Assigned, not defaulted. For an untrusted category the label
                # is forced, and a payload that already carries ``"trust":
                # "SYSTEM"`` is exactly the case this exists to defeat; for a
                # trusted one the value was read out of this same mapping, so
                # writing it back changes nothing.
                candidate[key] = trust
            if _json_estimate(candidate, estimator) <= budget:
                return candidate, False

    if isinstance(normalized, Mapping):
        result: dict[str, Any] = {}
        truncated = False
        for key in sorted(normalized, key=str):
            key_string = str(key)
            if key_string in trust_fields:
                result[key_string] = trust_fields[key_string]
                continue
            child, child_truncated = _project_json_value(
                normalized[key],
                budget=budget,
                category=category,
                estimator=estimator,
            )
            if child is _PROJECTION_UNAVAILABLE:
                truncated = True
                continue
            candidate = dict(result)
            candidate[key_string] = child
            if _json_estimate(candidate, estimator) <= budget:
                result[key_string] = child
                truncated = truncated or child_truncated
            else:
                truncated = True
        truncated = truncated or len(result) < len(normalized) or bool(trust_fields)
        if truncated:
            result["truncated"] = True
        for key, trust in trust_fields.items():
            result.setdefault(key, trust)
        result = _fit_mapping_to_budget(result, budget, estimator, trust_fields)
        if result is not None:
            return result, truncated
    elif isinstance(normalized, list):
        values: list[Any] = []
        truncated = False
        for child_value in normalized:
            child, child_truncated = _project_json_value(
                child_value,
                budget=budget,
                category=category,
                estimator=estimator,
            )
            if child is _PROJECTION_UNAVAILABLE:
                truncated = True
                break
            candidate = {"items": [*values, child], "truncated": True}
            if trust_fields:
                candidate.update(trust_fields)
            if _json_estimate(candidate, estimator) <= budget:
                values.append(child)
                truncated = truncated or child_truncated
            else:
                truncated = True
                break
        result = {"items": values, "truncated": True}
        result.update(trust_fields)
        result = _fit_mapping_to_budget(result, budget, estimator, trust_fields)
        if result is not None:
            return result, True
    else:
        result = {"value": normalized, "truncated": True}
        result.update(trust_fields)
        result = _fit_mapping_to_budget(result, budget, estimator, trust_fields)
        if result is not None:
            return result, True

    marker: dict[str, Any] = {"truncated": True}
    marker.update(trust_fields)
    if _json_estimate(marker, estimator) <= budget:
        return marker, True
    return _PROJECTION_UNAVAILABLE, True


def _fit_mapping_to_budget(
    value: dict[str, Any],
    budget: int,
    estimator: TokenEstimator,
    trust_fields: Mapping[str, Any],
) -> dict[str, Any] | None:
    if _json_estimate(value, estimator) <= budget:
        return value
    result = dict(value)
    for key in sorted(tuple(result), key=str, reverse=True):
        if key in trust_fields or key == "truncated":
            continue
        result.pop(key, None)
        if _json_estimate(result, estimator) <= budget:
            return result
    marker: dict[str, Any] = {"truncated": True}
    marker.update(trust_fields)
    return marker if _json_estimate(marker, estimator) <= budget else None


def _json_safe(value: Any) -> Any:
    """Normalize arbitrary provider/tool payloads to legal JSON values."""

    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return json.loads(encoded)
    except (TypeError, ValueError):
        if isinstance(value, Mapping):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [_json_safe(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value if not isinstance(value, float) or math.isfinite(value) else str(value)
        return str(value)


__all__ = [
    "CategorizedMessage",
    "ContextAdmissionResult",
    "ContextBudgetConfig",
    "ContextBudgetPolicy",
    "ContextBudgetUsage",
    "ContextCategory",
    "ContextItem",
    "ContextMessage",
    "TokenEstimator",
    "Utf8ByteTokenEstimator",
    "admit_context",
]
