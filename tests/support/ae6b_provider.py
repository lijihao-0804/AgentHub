"""Deterministic provider boundary for the AE-6b worker acceptance tests."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
    ModelToolCallDelta,
    ModelUsage,
)
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ProviderCredential
from packages.model_gateway.profile_resolution import ResolvedModelProfile
from packages.observability.contracts import TraceSink

AUDIT_PATH_ENV = "AGENTHUB_AE6B_AUDIT_PATH"

MARKER_CLARIFY = "AE6B:CLARIFY"
MARKER_REFUSE = "AE6B:REFUSE"
MARKER_DENIED = "AE6B:DENIED"
MARKER_PLAIN = "AE6B:PLAIN"
MARKER_UNKNOWN_USAGE = "AE6B:UNKNOWN_USAGE"
PUBLIC_MARKERS = (
    MARKER_CLARIFY,
    MARKER_REFUSE,
    MARKER_DENIED,
    MARKER_PLAIN,
    MARKER_UNKNOWN_USAGE,
)

CLARIFY_PROMPT = "Please provide your customer reference."
CLARIFY_COMPLETE = "The controlled support request is complete."
REFUSE_FALLBACK_PHRASE = "I do not have a customer reference."
REFUSE_RESPONSE = "I cannot proceed without the required customer reference."
DENIED_COMPLETE = "The ticket request was not executed because approval was denied."
PLAIN_RESPONSE = "Controlled AE6B response completed."
QUERY_FAILED_RESPONSE = "The customer lookup failed; no ticket was created."
TICKET_FAILED_RESPONSE = "The controlled ticket request did not complete successfully."
TOOL_UNAVAILABLE_RESPONSE = "The controlled AE6B tool is unavailable."
PENDING_RESULT_RESPONSE = "The controlled AE6B tool result is still pending."

_CUSTOMER_REF = re.compile(r"(?<![0-9A-Fa-f])AE6B-[0-9A-Fa-f]{32}(?![0-9A-Fa-f])")
HIDDEN_LABEL_PREFIX = "AE6B-HIDDEN-LABEL-"
_AUDIT_LOCK = threading.Lock()
_KNOWN_USAGE = ModelUsage(
    input_tokens=11,
    output_tokens=7,
    total_tokens=18,
    cached_tokens=0,
)


@dataclass(frozen=True, slots=True)
class _RequestSafetyProjection:
    user_turn_count: int
    user_ref_present: bool
    user_fixture_ref_present: bool
    fixture_ref_outside_user_before_disclosure: bool
    expected_label_present: bool


class AE6BDeterministicProviderAdapter:
    """Deterministic adapter that retains the real gateway and runtime path."""

    def __init__(self, *, audit_path: str | Path | None = None) -> None:
        configured = audit_path or os.environ.get(AUDIT_PATH_ENV)
        if not configured:
            raise RuntimeError(f"{AUDIT_PATH_ENV} is required for the AE6B provider")
        self.audit_path = Path(configured)

    async def complete(
        self,
        profile: ResolvedModelProfile,
        credential: ProviderCredential,
        request: ModelRequest,
    ) -> ModelResponse:
        safety_projection = _request_safety_projection(request)
        if safety_projection.expected_label_present:
            raise ValueError("AE6B_HIDDEN_LABEL_LEAK")
        response, marker = self._respond(profile, credential, request)
        self._append_audit(
            request=request,
            marker=marker,
            profile=profile,
            credential=credential,
            response=response,
            safety_projection=safety_projection,
        )
        return response

    async def stream(
        self,
        profile: ResolvedModelProfile,
        credential: ProviderCredential,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        safety_projection = _request_safety_projection(request)
        if safety_projection.expected_label_present:
            raise ValueError("AE6B_HIDDEN_LABEL_LEAK")
        response, marker = self._respond(profile, credential, request)
        self._append_audit(
            request=request,
            marker=marker,
            profile=profile,
            credential=credential,
            response=response,
            safety_projection=safety_projection,
        )
        if response.content:
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA,
                message_delta=response.content,
            )
        for index, call in enumerate(response.tool_calls):
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_call_delta=ModelToolCallDelta(
                    index=index,
                    name=call.name,
                    arguments_delta=json.dumps(
                        dict(call.arguments),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    provider_tool_call_id=call.provider_tool_call_id,
                ),
            )
        if response.usage is not None:
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.USAGE,
                usage=response.usage,
            )
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.COMPLETED,
            response=response,
        )

    async def health(
        self,
        profile: ResolvedModelProfile,
        credential: ProviderCredential,
    ) -> None:
        del profile, credential

    def _respond(
        self,
        profile: ResolvedModelProfile,
        credential: ProviderCredential,
        request: ModelRequest,
    ) -> tuple[ModelResponse, str]:
        marker = _request_marker(request)
        content, tool_calls = _response_content(marker, request)
        usage = None if marker == MARKER_UNKNOWN_USAGE else _KNOWN_USAGE
        response = ModelResponse(
            content=content,
            provider=credential.provider,
            model=profile.model,
            tool_calls=tool_calls,
            finish_reason="stop",
            usage=usage,
            cost_estimate=None,
        )
        return response, marker

    def _append_audit(
        self,
        *,
        request: ModelRequest,
        marker: str,
        profile: ResolvedModelProfile,
        credential: ProviderCredential,
        response: ModelResponse,
        safety_projection: _RequestSafetyProjection,
    ) -> None:
        usage = response.usage
        record = {
            "request_hash": _request_hash(request),
            "marker": marker,
            "provider": credential.provider,
            "model": profile.model,
            "pid": os.getpid(),
            "input_tokens": usage.input_tokens if usage is not None else None,
            "output_tokens": usage.output_tokens if usage is not None else None,
            "total_tokens": usage.total_tokens if usage is not None else None,
            "cached_tokens": usage.cached_tokens if usage is not None else None,
            "user_turn_count": safety_projection.user_turn_count,
            "user_ref_present": safety_projection.user_ref_present,
            "user_fixture_ref_present": safety_projection.user_fixture_ref_present,
            "fixture_ref_outside_user_before_disclosure": (
                safety_projection.fixture_ref_outside_user_before_disclosure
            ),
            "expected_label_present": safety_projection.expected_label_present,
        }
        line = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            with self.audit_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.write("\n")


def ae6b_model_gateway(
    session,
    *,
    settings: Settings,
    trace_sink: TraceSink | None,
) -> SqlAlchemyModelGateway:
    """Test-worker replacement for the production gateway composition."""

    return SqlAlchemyModelGateway(
        session,
        adapter=AE6BDeterministicProviderAdapter(),
        trace_sink=trace_sink,
        credential_cipher=ProviderCredentialCipher.from_settings(settings),
    )


def _response_content(
    marker: str,
    request: ModelRequest,
) -> tuple[str, tuple[ModelToolCall, ...]]:
    if marker in {MARKER_PLAIN, MARKER_UNKNOWN_USAGE}:
        return PLAIN_RESPONSE, ()

    user_turns = tuple(
        message.content or "" for message in request.messages if message.role == "user"
    )
    customer_ref = _customer_reference(user_turns)
    if marker == MARKER_REFUSE:
        if any(REFUSE_FALLBACK_PHRASE in text for text in user_turns):
            return REFUSE_RESPONSE, ()
        return CLARIFY_PROMPT, ()

    outcomes = _tool_outcomes(request.messages)
    proposed = _proposed_tool_names(request.messages)
    available = {tool.name for tool in request.tools}

    ticket_results = [payload for name, payload in outcomes if name == "create_ticket"]
    if ticket_results:
        payload = ticket_results[-1]
        if (
            str(payload.get("status", "")).upper() == "ERROR"
            and payload.get("error_code") == "TOOL_APPROVAL_DENIED"
        ):
            return DENIED_COMPLETE, ()
        if str(payload.get("status", "")).upper() == "SUCCESS":
            return CLARIFY_COMPLETE, ()
        return TICKET_FAILED_RESPONSE, ()
    if "create_ticket" in proposed:
        return PENDING_RESULT_RESPONSE, ()

    query_results = [payload for name, payload in outcomes if name == "query_customer"]
    if query_results:
        if str(query_results[-1].get("status", "")).upper() != "SUCCESS":
            return QUERY_FAILED_RESPONSE, ()
        if "create_ticket" not in available:
            return TOOL_UNAVAILABLE_RESPONSE, ()
        if customer_ref is None:
            return CLARIFY_PROMPT, ()
        return (
            "",
            (
                ModelToolCall(
                    name="create_ticket",
                    arguments={
                        "customer_ref": customer_ref,
                        "subject": "Controlled AE6b ticket",
                        "priority": "HIGH",
                    },
                    provider_tool_call_id="ae6b-create-ticket",
                ),
            ),
        )
    if "query_customer" in proposed:
        return PENDING_RESULT_RESPONSE, ()

    if customer_ref is None:
        return CLARIFY_PROMPT, ()
    if "query_customer" not in available:
        return TOOL_UNAVAILABLE_RESPONSE, ()
    return (
        "",
        (
            ModelToolCall(
                name="query_customer",
                arguments={"customer_ref": customer_ref},
                provider_tool_call_id="ae6b-query-customer",
            ),
        ),
    )


def _request_marker(request: ModelRequest) -> str:
    text = "\n".join(message.content or "" for message in request.messages)
    found = [marker for marker in PUBLIC_MARKERS if marker in text]
    if len(found) > 1:
        raise ValueError("AE6B_MULTIPLE_PUBLIC_MARKERS")
    if found:
        return found[0]
    user_messages = tuple(
        message.content or "" for message in request.messages if message.role == "user"
    )
    if user_messages and _CUSTOMER_REF.search(user_messages[-1]):
        return MARKER_CLARIFY
    return MARKER_PLAIN


def _customer_reference(user_turns: tuple[str, ...]) -> str | None:
    matches = _CUSTOMER_REF.findall("\n".join(user_turns))
    return matches[-1] if matches else None


def _request_safety_projection(request: ModelRequest) -> _RequestSafetyProjection:
    user_messages = tuple(message for message in request.messages if message.role == "user")
    message_text = tuple(_message_text(message) for message in request.messages)
    first_reference_index = next(
        (index for index, text in enumerate(message_text) if _CUSTOMER_REF.search(text)),
        None,
    )
    first_user_reference_index = next(
        (
            index
            for index, message in enumerate(request.messages)
            if message.role == "user" and _CUSTOMER_REF.search(message.content or "")
        ),
        None,
    )
    user_ref_present = first_user_reference_index is not None
    return _RequestSafetyProjection(
        user_turn_count=len(user_messages),
        user_ref_present=user_ref_present,
        user_fixture_ref_present=user_ref_present,
        fixture_ref_outside_user_before_disclosure=(
            first_reference_index is not None
            and (
                first_user_reference_index is None
                or first_reference_index < first_user_reference_index
            )
        ),
        expected_label_present=any(HIDDEN_LABEL_PREFIX in text for text in message_text),
    )


def _message_text(message: ModelMessage) -> str:
    tool_calls = "\n".join(
        json.dumps(
            {
                "name": call.name,
                "arguments": dict(call.arguments),
                "provider_tool_call_id": call.provider_tool_call_id,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        for call in message.tool_calls
    )
    return "\n".join(
        (
            message.content or "",
            message.name or "",
            message.tool_call_id or "",
            tool_calls,
        )
    )


def _tool_outcomes(
    messages: tuple[ModelMessage, ...],
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    call_names = {
        call.provider_tool_call_id: call.name
        for message in messages
        if message.role == "assistant"
        for call in message.tool_calls
        if call.provider_tool_call_id is not None
    }
    outcomes: list[tuple[str, Mapping[str, Any]]] = []
    for message in messages:
        if message.role != "tool":
            continue
        name = message.name or call_names.get(message.tool_call_id)
        if name is None:
            continue
        try:
            payload = json.loads(message.content or "")
        except (TypeError, ValueError):
            payload = {"status": "ERROR", "error_code": "TOOL_RESULT_INVALID"}
        if not isinstance(payload, dict):
            payload = {"status": "ERROR", "error_code": "TOOL_RESULT_INVALID"}
        outcomes.append((name, payload))
    return tuple(outcomes)


def _proposed_tool_names(messages: tuple[ModelMessage, ...]) -> set[str]:
    return {
        call.name
        for message in messages
        if message.role == "assistant"
        for call in message.tool_calls
    }


def _request_hash(request: ModelRequest) -> str:
    projection = {
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "name": message.name,
                "tool_call_id": message.tool_call_id,
                "tool_calls": [
                    {
                        "name": call.name,
                        "arguments": dict(call.arguments),
                        "provider_tool_call_id": call.provider_tool_call_id,
                    }
                    for call in message.tool_calls
                ],
            }
            for message in request.messages
        ],
        "tools": [
            {
                "name": tool.name,
                "parameters": dict(tool.parameters),
                "description": tool.description,
            }
            for tool in request.tools
        ],
        "response_schema": (
            {
                "name": request.response_schema.name,
                "json_schema": dict(request.response_schema.json_schema),
                "strict": request.response_schema.strict,
            }
            if request.response_schema is not None
            else None
        ),
        "required_capabilities": sorted(request.required_capabilities.required),
        "max_context_tokens": request.required_capabilities.max_context_tokens,
        "retry_policy": {"max_attempts": request.retry_policy.max_attempts},
        "request_mode": str(request.request_mode),
    }
    return canonical_json_hash(projection)


__all__ = [
    "AE6BDeterministicProviderAdapter",
    "AUDIT_PATH_ENV",
    "CLARIFY_COMPLETE",
    "CLARIFY_PROMPT",
    "DENIED_COMPLETE",
    "HIDDEN_LABEL_PREFIX",
    "MARKER_CLARIFY",
    "MARKER_DENIED",
    "MARKER_PLAIN",
    "MARKER_REFUSE",
    "MARKER_UNKNOWN_USAGE",
    "PLAIN_RESPONSE",
    "PUBLIC_MARKERS",
    "REFUSE_FALLBACK_PHRASE",
    "REFUSE_RESPONSE",
    "ae6b_model_gateway",
]
