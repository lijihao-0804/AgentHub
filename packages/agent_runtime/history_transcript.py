"""Strict, pure persistence contract for one complete thread Turn transcript.

The contract preserves the model-visible exchange needed to reconstruct the
current Turn:

* one user message;
* zero or more assistant tool-call groups, each followed by exactly one result
  for every call;
* one final assistant message without tool calls.

Parallel results retain their observed order.  A provider-generated call ID is
stored only so an assistant call and its result can be paired again; source
identity is always the outer ``(run_id, turn_id)`` pair.

Message contents and tool results are opaque, untrusted material.  This module
validates their types and JSON bounds but never interprets, logs, traces, or
executes them.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.model_gateway.contracts import ModelMessage, ModelToolCall

TRANSCRIPT_SCHEMA_VERSION = 1
TRANSCRIPT_PAYLOAD_KIND = "agenthub.turn_transcript"

MAX_ENCODED_BYTES = 4 * 1024 * 1024
MAX_TOOL_CALLS_PER_ASSISTANT = 32
MAX_TOTAL_TOOL_CALLS = 64
MAX_MESSAGES = 2 + 2 * MAX_TOTAL_TOOL_CALLS
MAX_MESSAGE_CONTENT_CHARS = 1_000_000
MAX_MESSAGE_NAME_CHARS = 256
MAX_PROTOCOL_CALL_ID_CHARS = 512
MAX_ARGUMENT_STRING_CHARS = 1_000_000
MAX_ARGUMENT_DEPTH = 16
MAX_ARGUMENT_NODES = 10_000
MAX_ARGUMENT_INTEGER_BITS = 1_024
MAX_PAYLOAD_DEPTH = 24
MAX_PAYLOAD_NODES = 100_000

_CANONICAL_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TOP_LEVEL_KEYS = frozenset({"schema_version", "kind", "run_id", "turn_id", "messages"})
_CALL_KEYS = frozenset({"protocol_call_id", "name", "arguments"})
_MESSAGE_KEYS = {
    "user": frozenset({"role", "content"}),
    "assistant_call": frozenset({"role", "content", "tool_calls"}),
    "assistant_final": frozenset({"role", "content"}),
    "tool": frozenset({"role", "content", "tool_call_id"}),
}


class TurnTranscriptValidationError(ValueError):
    """The Turn transcript contract or its serialized form is invalid."""


@dataclass(frozen=True, slots=True)
class TurnTranscript:
    """A complete current-Turn transcript with explicit Run/Turn identity."""

    run_id: UUID
    turn_id: UUID
    messages: tuple[ModelMessage, ...]

    def __post_init__(self) -> None:
        _require_uuid(self.run_id, "run_id")
        _require_uuid(self.turn_id, "turn_id")
        messages = _validate_turn_messages(self.messages)
        object.__setattr__(self, "messages", messages)
        encoded = _encode_payload(_payload_from_messages(self.run_id, self.turn_id, messages))
        if len(encoded) > MAX_ENCODED_BYTES:
            raise TurnTranscriptValidationError(
                f"encoded transcript exceeds {MAX_ENCODED_BYTES} UTF-8 bytes"
            )

    @property
    def model_messages(self) -> tuple[ModelMessage, ...]:
        """Return the recoverable ModelMessage sequence in transcript order."""

        return self.messages

    def to_payload(self) -> dict[str, Any]:
        """Return a detached, JSON-compatible, versioned payload."""

        return _payload_from_messages(self.run_id, self.turn_id, self.messages)

    def encode(self) -> str:
        """Return canonical JSON suitable for durable storage."""

        return _encode_payload(self.to_payload())

    @classmethod
    def from_payload(cls, payload: object) -> TurnTranscript:
        """Validate a JSON-compatible payload and restore ModelMessage values."""

        return _transcript_from_payload(payload)

    @classmethod
    def decode(cls, data: str | bytes) -> TurnTranscript:
        """Decode bounded UTF-8 JSON into a complete transcript."""

        return _decode_transcript(data)


def build_turn_transcript(
    *,
    run_id: UUID,
    turn_id: UUID,
    messages: Sequence[ModelMessage],
) -> TurnTranscript:
    """Build and validate one complete current-Turn transcript."""

    return TurnTranscript(run_id=run_id, turn_id=turn_id, messages=tuple(messages))


def validate_turn_messages(
    messages: Sequence[ModelMessage],
) -> tuple[ModelMessage, ...]:
    """Validate complete Turn shape and return the messages as a tuple."""

    return _validate_turn_messages(messages)


def transcript_to_payload(transcript: TurnTranscript) -> dict[str, Any]:
    """Return the versioned payload for a transcript."""

    if not isinstance(transcript, TurnTranscript):
        raise TypeError("transcript must be a TurnTranscript")
    return transcript.to_payload()


def transcript_from_payload(payload: object) -> TurnTranscript:
    """Restore a transcript from a decoded JSON payload."""

    return _transcript_from_payload(payload)


def encode_turn_transcript(transcript: TurnTranscript) -> str:
    """Encode a transcript as canonical JSON."""

    if not isinstance(transcript, TurnTranscript):
        raise TypeError("transcript must be a TurnTranscript")
    return transcript.encode()


def decode_turn_transcript(data: str | bytes) -> TurnTranscript:
    """Decode canonical or noncanonical JSON into a validated transcript."""

    return _decode_transcript(data)


def _validate_turn_messages(
    messages: Sequence[ModelMessage],
) -> tuple[ModelMessage, ...]:
    if isinstance(messages, (str, bytes, bytearray)) or not isinstance(messages, Sequence):
        raise TypeError("messages must be a sequence of ModelMessage values")
    values = tuple(messages)
    if any(not isinstance(message, ModelMessage) for message in values):
        raise TypeError("messages must contain ModelMessage values")
    if len(values) < 2:
        raise TurnTranscriptValidationError(
            "a complete transcript requires a user message and a final assistant message"
        )
    if len(values) > MAX_MESSAGES:
        raise TurnTranscriptValidationError(f"messages exceeds the limit of {MAX_MESSAGES}")

    first = values[0]
    if first.role != "user":
        raise TurnTranscriptValidationError("the first message must have role user")
    _validate_user_message(first)
    normalized: list[ModelMessage] = [
        ModelMessage(role="user", content=first.content, name=first.name)
    ]

    final = values[-1]
    if final.role != "assistant":
        raise TurnTranscriptValidationError("the final message must have role assistant")
    _validate_final_assistant(final)

    seen_call_ids: set[str] = set()
    total_calls = 0
    index = 1
    while index < len(values) - 1:
        assistant = values[index]
        if assistant.role != "assistant":
            if assistant.role == "tool":
                raise TurnTranscriptValidationError(
                    "orphan tool result has no preceding assistant tool-call group"
                )
            raise TurnTranscriptValidationError(
                "messages between the user and final assistant must be tool-call groups"
            )
        if assistant.tool_call_id is not None:
            raise TurnTranscriptValidationError("assistant messages must not carry tool_call_id")
        _validate_message_name(assistant.name)
        _validate_assistant_content(assistant.content)
        calls = tuple(assistant.tool_calls)
        if not calls:
            raise TurnTranscriptValidationError(
                "a non-final assistant message must contain tool calls"
            )
        if len(calls) > MAX_TOOL_CALLS_PER_ASSISTANT:
            raise TurnTranscriptValidationError("assistant tool calls exceed the per-message limit")
        total_calls += len(calls)
        if total_calls > MAX_TOTAL_TOOL_CALLS:
            raise TurnTranscriptValidationError("tool calls exceed the per-transcript limit")

        group_ids: list[str] = []
        call_names: dict[str, str] = {}
        normalized_calls: list[ModelToolCall] = []
        for call in calls:
            normalized_call = _validate_tool_call(call)
            call_id = normalized_call.provider_tool_call_id
            assert call_id is not None
            if call_id in seen_call_ids:
                raise TurnTranscriptValidationError(
                    "tool-call protocol IDs must be unique within a Turn"
                )
            seen_call_ids.add(call_id)
            group_ids.append(call_id)
            call_names[call_id] = normalized_call.name
            normalized_calls.append(normalized_call)

        normalized.append(
            ModelMessage(
                role="assistant",
                content=assistant.content,
                name=assistant.name,
                tool_calls=tuple(normalized_calls),
            )
        )

        result_start = index + 1
        result_end = result_start + len(group_ids)
        if result_end > len(values) - 1:
            raise TurnTranscriptValidationError(
                "assistant tool-call group is truncated before all results"
            )
        result_ids: list[str] = []
        for result in values[result_start:result_end]:
            if result.role != "tool":
                raise TurnTranscriptValidationError(
                    "assistant tool-call group is truncated before all results"
                )
            result_id = _validate_tool_result(result)
            if (
                result_id in call_names
                and result.name is not None
                and result.name != call_names[result_id]
            ):
                raise TurnTranscriptValidationError("tool result name does not match its call")
            result_ids.append(result_id)
            normalized.append(
                ModelMessage(
                    role="tool",
                    content=result.content,
                    name=result.name,
                    tool_call_id=result_id,
                )
            )
        if len(set(result_ids)) != len(result_ids):
            raise TurnTranscriptValidationError("tool results contain a duplicate protocol call ID")
        if set(result_ids) != set(group_ids):
            raise TurnTranscriptValidationError(
                "tool result protocol IDs do not match the assistant calls"
            )
        index = result_end

    normalized.append(ModelMessage(role="assistant", content=final.content, name=final.name))
    return tuple(normalized)


def _validate_user_message(message: ModelMessage) -> None:
    if message.tool_calls or message.tool_call_id is not None:
        raise TurnTranscriptValidationError(
            "the user message must not contain tool calls or tool_call_id"
        )
    _validate_message_name(message.name)
    _validate_content(message.content, "user content", allow_none=False, require_nonblank=True)


def _validate_final_assistant(message: ModelMessage) -> None:
    if message.tool_calls:
        raise TurnTranscriptValidationError(
            "the final assistant message must not contain tool calls"
        )
    if message.tool_call_id is not None:
        raise TurnTranscriptValidationError(
            "the final assistant message must not contain tool_call_id"
        )
    _validate_message_name(message.name)
    _validate_content(
        message.content,
        "final assistant content",
        allow_none=False,
        require_nonblank=True,
    )


def _validate_assistant_content(content: object) -> None:
    _validate_content(
        content,
        "assistant content",
        allow_none=True,
        require_nonblank=False,
    )


def _validate_tool_call(call: ModelToolCall) -> ModelToolCall:
    if not isinstance(call, ModelToolCall):
        raise TurnTranscriptValidationError(
            "assistant tool_calls must contain ModelToolCall values"
        )
    _require_name(call.name, "tool call name")
    call_id = _require_protocol_id(call.provider_tool_call_id, "tool call protocol ID")
    arguments = _normalize_arguments(call.arguments)
    return ModelToolCall(
        name=call.name,
        arguments=arguments,
        provider_tool_call_id=call_id,
    )


def _validate_tool_result(message: ModelMessage) -> str:
    if message.tool_calls:
        raise TurnTranscriptValidationError("tool result messages must not contain tool_calls")
    _validate_message_name(message.name)
    _validate_content(
        message.content,
        "tool result content",
        allow_none=False,
        require_nonblank=False,
    )
    return _require_protocol_id(message.tool_call_id, "tool result protocol ID")


def _payload_from_messages(
    run_id: UUID,
    turn_id: UUID,
    messages: Sequence[ModelMessage],
) -> dict[str, Any]:
    return {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "kind": TRANSCRIPT_PAYLOAD_KIND,
        "run_id": str(run_id),
        "turn_id": str(turn_id),
        "messages": [_message_payload(message) for message in messages],
    }


def _message_payload(message: ModelMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": message.role,
        "content": message.content,
    }
    if message.name is not None:
        payload["name"] = message.name
    if message.role == "assistant" and message.tool_calls:
        payload["tool_calls"] = [
            {
                "protocol_call_id": call.provider_tool_call_id,
                "name": call.name,
                "arguments": _normalize_arguments(call.arguments),
            }
            for call in message.tool_calls
        ]
    elif message.role == "tool":
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _transcript_from_payload(payload: object) -> TurnTranscript:
    if type(payload) is not dict:
        raise TurnTranscriptValidationError("payload must be a JSON object")
    _validate_payload_tree(payload)
    if set(payload) != _TOP_LEVEL_KEYS:
        raise TurnTranscriptValidationError(
            "payload must contain exactly schema_version, kind, run_id, turn_id, and messages"
        )
    if type(payload["schema_version"]) is not int:
        raise TurnTranscriptValidationError("schema_version must be an integer")
    if payload["schema_version"] != TRANSCRIPT_SCHEMA_VERSION:
        raise TurnTranscriptValidationError(
            f"unsupported transcript schema_version {payload['schema_version']!r}"
        )
    if payload["kind"] != TRANSCRIPT_PAYLOAD_KIND:
        raise TurnTranscriptValidationError("payload kind is invalid")
    run_id = _decode_uuid(payload["run_id"], "run_id")
    turn_id = _decode_uuid(payload["turn_id"], "turn_id")
    raw_messages = payload["messages"]
    if type(raw_messages) is not list:
        raise TurnTranscriptValidationError("messages must be a JSON array")
    if len(raw_messages) > MAX_MESSAGES:
        raise TurnTranscriptValidationError(f"messages exceeds the limit of {MAX_MESSAGES}")
    messages = tuple(_message_from_payload(item, index) for index, item in enumerate(raw_messages))
    return TurnTranscript(run_id=run_id, turn_id=turn_id, messages=messages)


def _message_from_payload(value: object, index: int) -> ModelMessage:
    if type(value) is not dict:
        raise TurnTranscriptValidationError(f"messages[{index}] must be a JSON object")
    role = value.get("role")
    if role == "assistant":
        shape = "assistant_call" if "tool_calls" in value else "assistant_final"
    elif role in {"user", "tool"}:
        shape = role
    else:
        raise TurnTranscriptValidationError(
            f"messages[{index}].role must be user, assistant, or tool"
        )
    expected_keys = _MESSAGE_KEYS[shape]
    actual_keys = set(value)
    if expected_keys - actual_keys or actual_keys - expected_keys - {"name"}:
        raise TurnTranscriptValidationError(
            f"messages[{index}] contains missing or unexpected fields"
        )

    name = value.get("name")
    if name is not None:
        _require_name(name, f"messages[{index}].name")
    content = value["content"]
    if role == "user":
        _validate_content(
            content,
            f"messages[{index}].content",
            allow_none=False,
            require_nonblank=True,
        )
        return ModelMessage(role="user", content=content, name=name)
    if role == "tool":
        _validate_content(
            content,
            f"messages[{index}].content",
            allow_none=False,
            require_nonblank=False,
        )
        tool_call_id = _require_protocol_id(
            value["tool_call_id"], f"messages[{index}].tool_call_id"
        )
        return ModelMessage(
            role="tool",
            content=content,
            name=name,
            tool_call_id=tool_call_id,
        )

    if shape == "assistant_call":
        _validate_assistant_content(content)
        raw_calls = value["tool_calls"]
        if type(raw_calls) is not list:
            raise TurnTranscriptValidationError(
                f"messages[{index}].tool_calls must be a JSON array"
            )
        if not raw_calls:
            raise TurnTranscriptValidationError(f"messages[{index}].tool_calls must not be empty")
        if len(raw_calls) > MAX_TOOL_CALLS_PER_ASSISTANT:
            raise TurnTranscriptValidationError(
                f"messages[{index}].tool_calls exceeds the per-message limit"
            )
        calls = tuple(
            _tool_call_from_payload(call, index=index, call_index=call_index)
            for call_index, call in enumerate(raw_calls)
        )
        return ModelMessage(
            role="assistant",
            content=content,
            name=name,
            tool_calls=calls,
        )

    _validate_content(
        content,
        f"messages[{index}].content",
        allow_none=False,
        require_nonblank=True,
    )
    return ModelMessage(role="assistant", content=content, name=name)


def _tool_call_from_payload(
    value: object,
    *,
    index: int,
    call_index: int,
) -> ModelToolCall:
    prefix = f"messages[{index}].tool_calls[{call_index}]"
    if type(value) is not dict:
        raise TurnTranscriptValidationError(f"{prefix} must be a JSON object")
    if set(value) != _CALL_KEYS:
        raise TurnTranscriptValidationError(
            f"{prefix} must contain exactly protocol_call_id, name, and arguments"
        )
    protocol_call_id = _require_protocol_id(value["protocol_call_id"], f"{prefix}.protocol_call_id")
    name = _require_name(value["name"], f"{prefix}.name")
    arguments = _normalize_arguments(value["arguments"])
    return ModelToolCall(
        name=name,
        arguments=arguments,
        provider_tool_call_id=protocol_call_id,
    )


def _decode_transcript(data: str | bytes) -> TurnTranscript:
    if isinstance(data, bytes):
        if len(data) > MAX_ENCODED_BYTES:
            raise TurnTranscriptValidationError(
                f"encoded transcript exceeds {MAX_ENCODED_BYTES} UTF-8 bytes"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise TurnTranscriptValidationError("encoded transcript must be valid UTF-8") from None
    elif isinstance(data, str):
        try:
            byte_length = len(data.encode("utf-8"))
        except UnicodeEncodeError:
            raise TurnTranscriptValidationError(
                "encoded transcript contains an invalid Unicode surrogate"
            ) from None
        if byte_length > MAX_ENCODED_BYTES:
            raise TurnTranscriptValidationError(
                f"encoded transcript exceeds {MAX_ENCODED_BYTES} UTF-8 bytes"
            )
        text = data
    else:
        raise TypeError("encoded transcript must be str or bytes")

    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except TurnTranscriptValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise TurnTranscriptValidationError("encoded transcript is not valid JSON") from None
    return _transcript_from_payload(payload)


def _encode_payload(payload: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded_bytes = encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise TurnTranscriptValidationError("transcript payload is not JSON-compatible") from None
    if len(encoded_bytes) > MAX_ENCODED_BYTES:
        raise TurnTranscriptValidationError(
            f"encoded transcript exceeds {MAX_ENCODED_BYTES} UTF-8 bytes"
        )
    return encoded


def _validate_payload_tree(payload: object) -> None:
    nodes = 0
    active: set[int] = set()

    def visit(value: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_PAYLOAD_NODES:
            raise TurnTranscriptValidationError("payload exceeds the JSON node limit")
        if depth > MAX_PAYLOAD_DEPTH:
            raise TurnTranscriptValidationError("payload exceeds the JSON depth limit")
        if value is None or type(value) in {bool, int, float, str}:
            if type(value) is int and value.bit_length() > MAX_ARGUMENT_INTEGER_BITS:
                raise TurnTranscriptValidationError("payload contains an oversized integer")
            if type(value) is float and not math.isfinite(value):
                raise TurnTranscriptValidationError("payload contains a non-finite number")
            return
        if type(value) not in {list, dict}:
            raise TurnTranscriptValidationError("payload must contain only JSON-compatible values")
        identity = id(value)
        if identity in active:
            raise TurnTranscriptValidationError("payload contains a cycle")
        active.add(identity)
        if type(value) is list:
            for item in value:
                visit(item, depth + 1)
        else:
            for key, item in value.items():
                nodes += 1
                if nodes > MAX_PAYLOAD_NODES:
                    raise TurnTranscriptValidationError("payload exceeds the JSON node limit")
                if type(key) is not str:
                    raise TurnTranscriptValidationError("payload object keys must be strings")
                if len(key) > MAX_MESSAGE_NAME_CHARS:
                    raise TurnTranscriptValidationError("payload contains an oversized object key")
                visit(item, depth + 1)
        active.remove(identity)

    visit(payload, 0)


def _normalize_arguments(arguments: object) -> dict[str, Any]:
    if type(arguments) is not dict and not isinstance(arguments, Mapping):
        raise TurnTranscriptValidationError("tool arguments must be a mapping")
    nodes = 0
    active: set[int] = set()

    def copy(value: object, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_ARGUMENT_NODES:
            raise TurnTranscriptValidationError("tool arguments exceed the node limit")
        if depth > MAX_ARGUMENT_DEPTH:
            raise TurnTranscriptValidationError("tool arguments exceed the depth limit")
        if value is None or type(value) is bool:
            return value
        if type(value) is int:
            if value.bit_length() > MAX_ARGUMENT_INTEGER_BITS:
                raise TurnTranscriptValidationError("tool arguments contain an oversized integer")
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise TurnTranscriptValidationError("tool arguments contain a non-finite number")
            return value
        if type(value) is str:
            if len(value) > MAX_ARGUMENT_STRING_CHARS:
                raise TurnTranscriptValidationError("tool arguments contain an oversized string")
            _ensure_valid_unicode(value, "tool argument string")
            return value
        if type(value) is list:
            identity = id(value)
            if identity in active:
                raise TurnTranscriptValidationError("tool arguments contain a cycle")
            active.add(identity)
            result = [copy(item, depth + 1) for item in value]
            active.remove(identity)
            return result
        if type(value) is dict:
            identity = id(value)
            if identity in active:
                raise TurnTranscriptValidationError("tool arguments contain a cycle")
            active.add(identity)
            result: dict[str, Any] = {}
            for key, item in value.items():
                nodes += 1
                if nodes > MAX_ARGUMENT_NODES:
                    raise TurnTranscriptValidationError("tool arguments exceed the node limit")
                if type(key) is not str:
                    raise TurnTranscriptValidationError("tool argument object keys must be strings")
                if len(key) > MAX_MESSAGE_NAME_CHARS:
                    raise TurnTranscriptValidationError(
                        "tool arguments contain an oversized object key"
                    )
                _ensure_valid_unicode(key, "tool argument key")
                result[key] = copy(item, depth + 1)
            active.remove(identity)
            return result
        raise TurnTranscriptValidationError(
            "tool arguments must contain only JSON-compatible values"
        )

    try:
        detached = dict(arguments)
    except (TypeError, ValueError):
        raise TurnTranscriptValidationError("tool arguments must be a mapping") from None
    try:
        result = copy(detached, 0)
    except RecursionError:
        raise TurnTranscriptValidationError("tool arguments exceed the depth limit") from None
    assert isinstance(result, dict)
    return result


def _validate_content(
    content: object,
    field: str,
    *,
    allow_none: bool,
    require_nonblank: bool,
) -> None:
    if content is None:
        if allow_none:
            return
        raise TurnTranscriptValidationError(f"{field} must be a string")
    if type(content) is not str:
        raise TurnTranscriptValidationError(f"{field} must be a string")
    if len(content) > MAX_MESSAGE_CONTENT_CHARS:
        raise TurnTranscriptValidationError(
            f"{field} exceeds {MAX_MESSAGE_CONTENT_CHARS} characters"
        )
    _ensure_valid_unicode(content, field)
    if require_nonblank and not content.strip():
        raise TurnTranscriptValidationError(f"{field} must not be blank")


def _validate_message_name(name: object) -> None:
    if name is not None:
        _require_name(name, "message name")


def _require_name(value: object, field: str) -> str:
    if type(value) is not str:
        raise TurnTranscriptValidationError(f"{field} must be a string")
    if not value or value != value.strip():
        raise TurnTranscriptValidationError(
            f"{field} must be non-empty and have no surrounding whitespace"
        )
    if len(value) > MAX_MESSAGE_NAME_CHARS:
        raise TurnTranscriptValidationError(f"{field} exceeds {MAX_MESSAGE_NAME_CHARS} characters")
    _ensure_valid_unicode(value, field)
    if any(not character.isprintable() for character in value):
        raise TurnTranscriptValidationError(f"{field} must contain only printable characters")
    return value


def _require_protocol_id(value: object, field: str) -> str:
    if type(value) is not str:
        raise TurnTranscriptValidationError(f"{field} must be a string")
    if not value or value != value.strip():
        raise TurnTranscriptValidationError(
            f"{field} must be non-empty and have no surrounding whitespace"
        )
    if len(value) > MAX_PROTOCOL_CALL_ID_CHARS:
        raise TurnTranscriptValidationError(
            f"{field} exceeds {MAX_PROTOCOL_CALL_ID_CHARS} characters"
        )
    _ensure_valid_unicode(value, field)
    if any(not character.isprintable() or character.isspace() for character in value):
        raise TurnTranscriptValidationError(
            f"{field} must contain only printable non-whitespace characters"
        )
    return value


def _ensure_valid_unicode(value: str, field: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise TurnTranscriptValidationError(
            f"{field} contains an invalid Unicode surrogate"
        ) from None
    if "\x00" in value:
        raise TurnTranscriptValidationError(f"{field} must not contain NUL")


def _require_uuid(value: object, field: str) -> UUID:
    if type(value) is not UUID:
        raise TurnTranscriptValidationError(f"{field} must be a UUID")
    return value


def _decode_uuid(value: object, field: str) -> UUID:
    if type(value) is not str or not _CANONICAL_UUID.fullmatch(value):
        raise TurnTranscriptValidationError(f"payload {field} must be a canonical lowercase UUID")
    try:
        parsed = UUID(value)
    except ValueError:
        raise TurnTranscriptValidationError(
            f"payload {field} must be a canonical lowercase UUID"
        ) from None
    if str(parsed) != value:
        raise TurnTranscriptValidationError(f"payload {field} must be a canonical lowercase UUID")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TurnTranscriptValidationError(
                "encoded transcript contains a duplicate JSON object key"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise TurnTranscriptValidationError(f"encoded transcript contains invalid JSON number {value}")


__all__ = [
    "MAX_ARGUMENT_DEPTH",
    "MAX_ARGUMENT_NODES",
    "MAX_ARGUMENT_STRING_CHARS",
    "MAX_ENCODED_BYTES",
    "MAX_MESSAGE_CONTENT_CHARS",
    "MAX_MESSAGE_NAME_CHARS",
    "MAX_MESSAGES",
    "MAX_PROTOCOL_CALL_ID_CHARS",
    "MAX_TOOL_CALLS_PER_ASSISTANT",
    "MAX_TOTAL_TOOL_CALLS",
    "TRANSCRIPT_PAYLOAD_KIND",
    "TRANSCRIPT_SCHEMA_VERSION",
    "TurnTranscript",
    "TurnTranscriptValidationError",
    "build_turn_transcript",
    "decode_turn_transcript",
    "encode_turn_transcript",
    "transcript_from_payload",
    "transcript_to_payload",
    "validate_turn_messages",
]
