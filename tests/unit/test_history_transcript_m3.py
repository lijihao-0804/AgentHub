"""Tests for the complete current-Turn history transcript contract."""

from __future__ import annotations

import json
from math import nan
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.history_transcript import (
    MAX_ARGUMENT_DEPTH,
    MAX_ENCODED_BYTES,
    TRANSCRIPT_PAYLOAD_KIND,
    TRANSCRIPT_SCHEMA_VERSION,
    TurnTranscript,
    TurnTranscriptValidationError,
    build_turn_transcript,
    decode_turn_transcript,
    encode_turn_transcript,
    validate_turn_messages,
)
from packages.agent_runtime.work_layer import ThreadHistorySourceIncompleteError
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from packages.threads.context import _complete_transcript


def _call(name: str, call_id: str, arguments: dict | None = None) -> ModelToolCall:
    return ModelToolCall(
        name=name,
        arguments=arguments or {},
        provider_tool_call_id=call_id,
    )


def _parallel_messages() -> tuple[ModelMessage, ...]:
    return (
        ModelMessage(
            role="user",
            content="Compare both sources.",
            name="request",
        ),
        ModelMessage(
            role="assistant",
            content=None,
            name="planner",
            tool_calls=(
                _call("search", "call-a", {"query": "alpha", "limit": 2}),
                _call("search", "call-b", {"query": "beta", "limit": [1, 2]}),
            ),
        ),
        ModelMessage(
            role="tool",
            content='{"source":"beta"}',
            name="search",
            tool_call_id="call-b",
        ),
        ModelMessage(
            role="tool",
            content='{"source":"alpha"}',
            name="search",
            tool_call_id="call-a",
        ),
        ModelMessage(
            role="assistant",
            content="Alpha and beta differ.",
            name="writer",
        ),
    )


def test_round_trip_preserves_parallel_result_order_and_original_fields() -> None:
    run_id = uuid4()
    turn_id = uuid4()
    transcript = build_turn_transcript(
        run_id=run_id,
        turn_id=turn_id,
        messages=_parallel_messages(),
    )

    payload = transcript.to_payload()
    encoded = encode_turn_transcript(transcript)
    restored = decode_turn_transcript(encoded)

    assert payload["schema_version"] == TRANSCRIPT_SCHEMA_VERSION
    assert payload["kind"] == TRANSCRIPT_PAYLOAD_KIND
    assert payload["run_id"] == str(run_id)
    assert payload["turn_id"] == str(turn_id)
    assert "source_id" not in payload
    assert [message["role"] for message in payload["messages"]] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert payload["messages"][2]["tool_call_id"] == "call-b"
    assert payload["messages"][3]["tool_call_id"] == "call-a"
    assert restored == transcript
    assert restored.model_messages == _parallel_messages()
    assert restored.messages[1].tool_calls[0].arguments == {
        "query": "alpha",
        "limit": 2,
    }
    assert json.loads(encoded) == payload


def test_result_name_must_match_the_corresponding_call() -> None:
    messages = list(_parallel_messages())
    messages[2] = ModelMessage(
        role="tool", content="result", name="different-tool", tool_call_id="call-b"
    )
    with pytest.raises(TurnTranscriptValidationError):
        build_turn_transcript(run_id=uuid4(), turn_id=uuid4(), messages=messages)


def test_truncated_tool_result_cannot_be_replayed_as_complete_history() -> None:
    run_id, turn_id = uuid4(), uuid4()
    messages = (
        ModelMessage("user", "question"),
        ModelMessage("assistant", tool_calls=(_call("lookup", "call-1"),)),
        ModelMessage("tool", '{"truncated":true}', name="lookup", tool_call_id="call-1"),
        ModelMessage("assistant", "answer"),
    )
    payload = build_turn_transcript(run_id=run_id, turn_id=turn_id, messages=messages).to_payload()
    turn = SimpleNamespace(id=turn_id, user_input="question")
    run = SimpleNamespace(
        id=run_id, final_output="answer", tool_call_count=1, thread_history_transcript=payload
    )
    with pytest.raises(ThreadHistorySourceIncompleteError):
        _complete_transcript(turn, run)


def test_rag_history_without_authorized_reference_registry_fails_closed() -> None:
    run_id, turn_id = uuid4(), uuid4()
    messages = (
        ModelMessage("user", "question"),
        ModelMessage("assistant", tool_calls=(_call("search_knowledge", "call-1"),)),
        ModelMessage("tool", '{"status":"SUCCESS"}', tool_call_id="call-1"),
        ModelMessage("assistant", "answer"),
    )
    payload = build_turn_transcript(run_id=run_id, turn_id=turn_id, messages=messages).to_payload()
    turn = SimpleNamespace(id=turn_id, user_input="question")
    run = SimpleNamespace(
        id=run_id, final_output="answer", tool_call_count=1, thread_history_transcript=payload
    )
    with pytest.raises(ThreadHistorySourceIncompleteError):
        _complete_transcript(turn, run)


def test_multiple_tool_call_groups_are_validated_as_complete_atomic_groups() -> None:
    messages = (
        ModelMessage(role="user", content="First lookup, then calculate."),
        ModelMessage(
            role="assistant",
            content="Looking up.",
            tool_calls=(_call("lookup", "call-1", {"id": 7}),),
        ),
        ModelMessage(role="tool", content="value", tool_call_id="call-1"),
        ModelMessage(
            role="assistant",
            content="Calculating.",
            tool_calls=(_call("calculate", "call-2", {"value": 7}),),
        ),
        ModelMessage(role="tool", content="14", tool_call_id="call-2"),
        ModelMessage(role="assistant", content="The result is 14."),
    )

    transcript = TurnTranscript(
        run_id=uuid4(),
        turn_id=uuid4(),
        messages=messages,
    )
    restored = TurnTranscript.decode(transcript.encode())

    assert restored.messages == messages
    assert len(restored.messages) == 6


@pytest.mark.parametrize(
    "messages",
    [
        (
            ModelMessage(role="user", content="question"),
            ModelMessage(
                role="assistant",
                tool_calls=(
                    _call("search", "call-a"),
                    _call("search", "call-b"),
                ),
            ),
            ModelMessage(role="tool", content="a", tool_call_id="call-a"),
            ModelMessage(role="assistant", content="answer"),
        ),
        (
            ModelMessage(role="user", content="question"),
            ModelMessage(
                role="assistant",
                tool_calls=(
                    _call("search", "call-a"),
                    _call("search", "call-b"),
                ),
            ),
            ModelMessage(role="tool", content="a", tool_call_id="call-a"),
            ModelMessage(role="tool", content="again", tool_call_id="call-a"),
            ModelMessage(role="assistant", content="answer"),
        ),
        (
            ModelMessage(role="user", content="question"),
            ModelMessage(
                role="assistant",
                tool_calls=(
                    _call("search", "call-a"),
                    _call("search", "call-b"),
                ),
            ),
            ModelMessage(role="tool", content="a", tool_call_id="call-a"),
            ModelMessage(role="tool", content="c", tool_call_id="call-c"),
            ModelMessage(role="assistant", content="answer"),
        ),
        (
            ModelMessage(role="user", content="question"),
            ModelMessage(
                role="assistant",
                tool_calls=(_call("search", "call-a"),),
            ),
            ModelMessage(role="tool", content="a", tool_call_id="call-a"),
            ModelMessage(role="tool", content="orphan", tool_call_id="call-b"),
            ModelMessage(role="assistant", content="answer"),
        ),
        (
            ModelMessage(role="user", content="question"),
            ModelMessage(
                role="assistant",
                tool_calls=(
                    _call("search", "call-a"),
                    _call("search", "call-b"),
                ),
            ),
            ModelMessage(role="tool", content="a", tool_call_id="call-a"),
        ),
    ],
)
def test_missing_duplicate_mismatched_orphan_and_truncated_groups_fail(
    messages: tuple[ModelMessage, ...],
) -> None:
    with pytest.raises(TurnTranscriptValidationError):
        validate_turn_messages(messages)


def test_duplicate_protocol_ids_across_rounds_are_invalid() -> None:
    messages = (
        ModelMessage(role="user", content="question"),
        ModelMessage(
            role="assistant",
            tool_calls=(_call("first", "duplicate"),),
        ),
        ModelMessage(role="tool", content="one", tool_call_id="duplicate"),
        ModelMessage(
            role="assistant",
            tool_calls=(_call("second", "duplicate"),),
        ),
        ModelMessage(role="tool", content="two", tool_call_id="duplicate"),
        ModelMessage(role="assistant", content="answer"),
    )

    with pytest.raises(TurnTranscriptValidationError, match="unique"):
        validate_turn_messages(messages)


def test_source_identity_is_only_the_typed_outer_run_and_turn_pair() -> None:
    with pytest.raises(TurnTranscriptValidationError, match="run_id"):
        TurnTranscript(
            run_id="not-a-uuid",  # type: ignore[arg-type]
            turn_id=uuid4(),
            messages=_parallel_messages(),
        )

    transcript = build_turn_transcript(
        run_id=uuid4(),
        turn_id=uuid4(),
        messages=_parallel_messages(),
    )
    payload = transcript.to_payload()
    payload["source_id"] = "provider-or-client-controlled"
    with pytest.raises(TurnTranscriptValidationError, match="exactly"):
        TurnTranscript.from_payload(payload)

    payload = transcript.to_payload()
    payload["messages"][1]["tool_calls"][0]["source_id"] = "provider-or-client-controlled"
    with pytest.raises(TurnTranscriptValidationError, match="exactly"):
        TurnTranscript.from_payload(payload)

    malformed = transcript.to_payload()
    malformed["run_id"] = str(uuid4()).upper()
    with pytest.raises(TurnTranscriptValidationError, match="canonical lowercase UUID"):
        TurnTranscript.from_payload(malformed)


def test_protocol_id_is_required_for_pairing_and_is_not_a_source_identity() -> None:
    messages = (
        ModelMessage(role="user", content="question"),
        ModelMessage(
            role="assistant",
            tool_calls=(ModelToolCall(name="search", arguments={}),),
        ),
        ModelMessage(role="tool", content="result", tool_call_id="call-1"),
        ModelMessage(role="assistant", content="answer"),
    )

    with pytest.raises(TurnTranscriptValidationError, match="protocol ID"):
        validate_turn_messages(messages)


def test_payload_rejects_duplicate_keys_nonfinite_numbers_and_unknown_fields() -> None:
    transcript = build_turn_transcript(
        run_id=uuid4(),
        turn_id=uuid4(),
        messages=_parallel_messages(),
    )
    encoded = transcript.encode()

    duplicate = encoded.replace(
        '"run_id":',
        f'"run_id":"{uuid4()}","run_id":',
        1,
    )
    with pytest.raises(TurnTranscriptValidationError, match="duplicate"):
        decode_turn_transcript(duplicate)

    payload = transcript.to_payload()
    payload["messages"][1]["tool_calls"][0]["arguments"]["bad"] = nan
    with pytest.raises(TurnTranscriptValidationError, match="non-finite"):
        TurnTranscript.from_payload(payload)

    payload = transcript.to_payload()
    payload["messages"][0]["unexpected"] = "ignored?"
    with pytest.raises(TurnTranscriptValidationError, match="missing or unexpected"):
        TurnTranscript.from_payload(payload)


def test_types_depth_and_encoded_size_are_strictly_bounded() -> None:
    deep: object = "leaf"
    for _ in range(MAX_ARGUMENT_DEPTH + 1):
        deep = [deep]
    invalid_arguments = (
        ModelMessage(role="user", content="question"),
        ModelMessage(
            role="assistant",
            tool_calls=(
                ModelToolCall(
                    name="deep",
                    arguments={"value": deep},
                    provider_tool_call_id="call-deep",
                ),
            ),
        ),
        ModelMessage(role="tool", content="result", tool_call_id="call-deep"),
        ModelMessage(role="assistant", content="answer"),
    )
    with pytest.raises(TurnTranscriptValidationError, match="depth"):
        validate_turn_messages(invalid_arguments)

    with pytest.raises(TurnTranscriptValidationError, match="JSON-compatible"):
        validate_turn_messages(
            (
                ModelMessage(role="user", content="question"),
                ModelMessage(
                    role="assistant",
                    tool_calls=(
                        ModelToolCall(
                            name="bad",
                            arguments={"value": object()},
                            provider_tool_call_id="call-bad",
                        ),
                    ),
                ),
                ModelMessage(role="tool", content="result", tool_call_id="call-bad"),
                ModelMessage(role="assistant", content="answer"),
            )
        )

    with pytest.raises(TurnTranscriptValidationError, match="NUL"):
        validate_turn_messages(
            (
                ModelMessage(role="user", content="question"),
                ModelMessage(
                    role="assistant",
                    tool_calls=(
                        ModelToolCall(
                            name="bad",
                            arguments={"value": "unsafe\x00value"},
                            provider_tool_call_id="call-nul",
                        ),
                    ),
                ),
                ModelMessage(role="tool", content="result", tool_call_id="call-nul"),
                ModelMessage(role="assistant", content="answer"),
            )
        )

    with pytest.raises(TurnTranscriptValidationError, match="UTF-8 bytes"):
        decode_turn_transcript(b" " * (MAX_ENCODED_BYTES + 1))


def test_final_assistant_is_required_and_must_be_terminal() -> None:
    with pytest.raises(TurnTranscriptValidationError, match="final"):
        validate_turn_messages((ModelMessage(role="user", content="question"),))

    with pytest.raises(TurnTranscriptValidationError, match="final"):
        validate_turn_messages(
            (
                ModelMessage(role="user", content="question"),
                ModelMessage(
                    role="assistant",
                    tool_calls=(_call("search", "call-1"),),
                ),
            )
        )

    with pytest.raises(TurnTranscriptValidationError, match="final"):
        validate_turn_messages(
            (
                ModelMessage(role="user", content="question"),
                ModelMessage(role="assistant", content="answer"),
                ModelMessage(role="user", content="another question"),
            )
        )


def test_payload_uses_canonical_uuid_text_and_is_detached() -> None:
    run_id = UUID("00000000-0000-4000-8000-000000000001")
    transcript = build_turn_transcript(
        run_id=run_id,
        turn_id=uuid4(),
        messages=_parallel_messages(),
    )

    first = transcript.to_payload()
    first["run_id"] = "changed"
    second = transcript.to_payload()

    assert second["run_id"] == str(run_id)


def test_arguments_are_detached_from_the_caller_s_mutable_input() -> None:
    arguments = {"query": "original", "nested": {"limit": 1}}
    messages = (
        ModelMessage(role="user", content="question"),
        ModelMessage(
            role="assistant",
            tool_calls=(_call("search", "call-1", arguments),),
        ),
        ModelMessage(role="tool", content="result", tool_call_id="call-1"),
        ModelMessage(role="assistant", content="answer"),
    )
    transcript = build_turn_transcript(
        run_id=uuid4(),
        turn_id=uuid4(),
        messages=messages,
    )

    arguments["query"] = "mutated"
    arguments["nested"]["limit"] = 99  # type: ignore[index]

    assert transcript.messages[1].tool_calls[0].arguments == {
        "query": "original",
        "nested": {"limit": 1},
    }
