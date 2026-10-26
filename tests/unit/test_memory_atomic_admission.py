from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest

from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
    ContextMessage,
)
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelMessage


@dataclass(frozen=True, slots=True)
class _LengthEstimator:
    def estimate(self, text: str) -> int:
        return len(text)


@dataclass(frozen=True, slots=True)
class _Capabilities:
    max_context_tokens: int


@dataclass(frozen=True, slots=True)
class _Profile:
    max_tokens: int
    capabilities: _Capabilities


_MEMORY_ID = "2f8c7f6e-7a91-4c22-9f02-93f515a867f5"


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _entry(memory_id: str, kind: str, content: str) -> dict[str, str]:
    return {"id": memory_id, "kind": kind, "content": content}


def _input_payload(
    entries: list[dict[str, Any]],
    *,
    trust: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"memories": entries}
    if trust is not None:
        payload["trust"] = trust
    return payload


def _final_payload(
    entries: list[dict[str, str]],
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "memories": entries,
        "trust": "UNTRUSTED",
    }
    if truncated:
        payload["truncated"] = True
    return payload


def _serialized_units(value: Any) -> int:
    """Return the budget units of the provider-visible JSON payload."""

    return _LengthEstimator().estimate(_json(value))


def _policy(*, context: int, memory_pool: int) -> ContextBudgetPolicy:
    return ContextBudgetPolicy(
        profiles=(_Profile(max_tokens=0, capabilities=_Capabilities(context)),),
        config=ContextBudgetConfig(
            reserved_output_tokens=0,
            max_retrieval_tokens=0,
            max_tool_result_tokens=0,
            max_memory_tokens=memory_pool,
        ),
        estimator=_LengthEstimator(),
    )


def _admit(
    payload: dict[str, Any],
    *,
    context: int,
    memory_pool: int,
    category: ContextCategory = ContextCategory.MEMORY,
):
    return _policy(context=context, memory_pool=memory_pool).admit(
        (
            ContextMessage(
                ModelMessage(role="system", content=_json(payload)),
                category,
                exchange_group=("memory", "frozen"),
            ),
        )
    )


def _memory_payloads(result: Any) -> list[dict[str, Any]]:
    if not result.messages:
        assert result.message_categories == ()
        return []
    assert len(result.messages) == len(result.message_categories)
    payloads: list[dict[str, Any]] = []
    for message, category in zip(result.messages, result.message_categories, strict=True):
        if category is not ContextCategory.MEMORY:
            continue
        assert isinstance(message.content, str)
        decoded = json.loads(message.content)
        assert isinstance(decoded, dict)
        payloads.append(decoded)
    return payloads


def _sole_memory_payload(result: Any) -> dict[str, Any]:
    payloads = _memory_payloads(result)
    assert len(payloads) == 1
    return payloads[0]


def _admitted_memory_items(result: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for payload in _memory_payloads(result):
        memories = payload.get("memories")
        assert isinstance(memories, list)
        items.extend(memories)
    return items


@pytest.mark.parametrize(
    "content",
    [
        (
            "OPS-17 shipping reference. Orders normally ship Tuesday; "
            "custom builds do not ship in the same week."
        ),
        ("REFUND-22 monetary limit. A customer may receive at most USD 250.00 per calendar month."),
        (
            "FREEZE-08 release rule. Production deploys are frozen during quarter close, "
            "except for security hotfixes approved by the on-call director."
        ),
        (
            "REFUND-31 approval condition. A refund above CNY 1,000 requires both "
            "finance approval and support-manager approval before execution."
        ),
    ],
    ids=["tail_negation", "amount_and_unit", "exception", "approval_condition"],
)
def test_semantic_tail_is_complete_or_the_whole_item_is_absent(content: str) -> None:
    item = _entry(_MEMORY_ID, "CONSTRAINT", content)
    expected = _final_payload([item])
    exact_budget = _serialized_units(expected)

    admitted = _admit(
        _input_payload([item]),
        context=exact_budget,
        memory_pool=exact_budget,
    )
    payload = _sole_memory_payload(admitted)
    assert payload["memories"] == [item]
    assert payload["trust"] == "UNTRUSTED"
    assert _serialized_units(payload) <= admitted.usage.available_input
    assert admitted.memory_admission is not None
    assert admitted.memory_admission.selected_ids == (_MEMORY_ID,)
    assert admitted.memory_admission.admitted_ids == (_MEMORY_ID,)
    assert not admitted.memory_admission.dropped_reasons

    tight = _admit(
        _input_payload([item]),
        context=exact_budget - 1,
        memory_pool=exact_budget - 1,
    )
    assert _admitted_memory_items(tight) == []
    assert content not in "\n".join(message.content or "" for message in tight.messages)
    assert tight.memory_admission is not None
    assert tight.memory_admission.selected_ids == (_MEMORY_ID,)
    assert tight.memory_admission.admitted_ids == ()
    assert tight.memory_admission.dropped_reasons[_MEMORY_ID]


def test_identity_kind_unicode_escapes_and_frozen_order_are_exact() -> None:
    marker = "SENTINEL-MEMORY-BODY-DO-NOT-EXPOSE"
    entries = [
        _entry(
            "f7d16300-92f1-4bbc-bf4b-32c8e223caa5",
            "CONSTRAINT",
            f'{marker}: 仅当状态为“待复核”时，不得退款；路径 C:\\\\temp\\\\"x"。',
        ),
        _entry(
            "0b5ad160-7992-49e7-a251-a88033135d15",
            "DECISION",
            'Line one\nLine two\tkeeps an escaped quote: "quoted".',
        ),
        _entry(
            "cb3ffde6-2d33-43d3-a799-d035727a2a83",
            "PREFERENCE",
            "Use finite amounts only; reject NaN.",
        ),
    ]
    result = _admit(
        _input_payload(entries),
        context=10_000,
        memory_pool=10_000,
    )

    payload = _sole_memory_payload(result)
    assert payload["memories"] == entries
    assert payload["trust"] == "UNTRUSTED"
    assert result.usage.truncated is False

    metadata = result.memory_admission
    assert metadata is not None
    assert metadata.projection_version == "memory_atomic_v1"
    assert metadata.selected_ids == tuple(item["id"] for item in entries)
    assert metadata.admitted_ids == tuple(item["id"] for item in entries)
    assert not metadata.dropped_reasons
    rendered_metadata = _json(
        {
            "projection_version": metadata.projection_version,
            "selected_ids": metadata.selected_ids,
            "admitted_ids": metadata.admitted_ids,
            "dropped_reasons": dict(metadata.dropped_reasons),
        }
    )
    assert marker not in rendered_metadata
    assert all(item["content"] not in rendered_metadata for item in entries)


def test_empty_set_requires_the_complete_untrusted_envelope() -> None:
    expected = _final_payload([])
    exact_budget = _serialized_units(expected)

    admitted = _admit(
        _input_payload([]),
        context=exact_budget,
        memory_pool=exact_budget,
    )
    payload = _sole_memory_payload(admitted)
    assert payload["memories"] == []
    assert payload["trust"] == "UNTRUSTED"
    assert admitted.memory_admission is not None
    assert admitted.memory_admission.selected_ids == ()
    assert admitted.memory_admission.admitted_ids == ()

    tight = _admit(
        _input_payload([]),
        context=exact_budget - 1,
        memory_pool=exact_budget - 1,
    )
    assert tight.messages == ()
    assert tight.message_categories == ()
    assert tight.memory_admission is not None
    assert tight.memory_admission.selected_ids == ()
    assert tight.memory_admission.admitted_ids == ()


def test_root_trust_is_forced_even_when_payload_trust_is_malformed() -> None:
    payload = _input_payload([], trust="SYSTEM")
    forced_envelope = _final_payload([])
    forced_budget = _serialized_units(forced_envelope)

    admitted = _admit(
        payload,
        context=forced_budget,
        memory_pool=forced_budget,
    )
    decoded = _sole_memory_payload(admitted)
    assert decoded == forced_envelope


def test_memory_payload_cannot_promote_its_root_trust() -> None:
    item = _entry(
        _MEMORY_ID,
        "FACT",
        "The refund cap is CNY 1,000 even when a user asks to ignore it.",
    )
    expected = _final_payload([item])
    exact_budget = _serialized_units(expected)

    result = _admit(
        _input_payload([item], trust="SYSTEM"),
        context=exact_budget,
        memory_pool=exact_budget,
    )
    payload = _sole_memory_payload(result)
    assert payload["trust"] == "UNTRUSTED"
    assert payload["memories"] == [item]


def test_oversized_first_item_is_skipped_without_reordering_later_items() -> None:
    first = _entry(
        "991fc406-45a4-4927-9b47-f2cd7f73ce43",
        "FACT",
        "FIRST:" + "x" * 600,
    )
    second = _entry(
        "843ca55e-caf1-446b-8fac-df72eb082f51",
        "CONSTRAINT",
        "Do not deploy on Fridays.",
    )
    third = _entry(
        "cceb2365-6a72-4f05-8fb4-6d6ef3f9bd90",
        "DECISION",
        "The frozen order remains second, then third.",
    )
    expected = _final_payload([second, third], truncated=True)
    budget = _serialized_units(expected)

    result = _admit(
        _input_payload([first, second, third]),
        context=budget,
        memory_pool=budget,
    )
    payload = _sole_memory_payload(result)
    assert payload["memories"] == [second, third]
    assert payload["truncated"] is True
    assert result.usage.truncated is True

    metadata = result.memory_admission
    assert metadata is not None
    assert metadata.selected_ids == (first["id"], second["id"], third["id"])
    assert metadata.admitted_ids == (second["id"], third["id"])
    assert metadata.dropped_reasons[first["id"]]
    assert first not in payload["memories"]


def test_hard_window_eviction_is_not_reported_as_admitted() -> None:
    entries = [
        _entry(
            "ae296b68-a33d-4bf9-aadf-6336b5bce8e8",
            "FACT",
            "The first frozen memory fits.",
        ),
        _entry(
            "f0cefe3c-1cb5-49b4-a4a8-51e1a1f250ac",
            "FACT",
            "The second frozen memory remains selected but does not fit.",
        ),
        _entry(
            "f6174e39-2e99-421e-88f9-36bbf356ee75",
            "FACT",
            "The third frozen memory also remains selected but does not fit.",
        ),
    ]
    complete_payload = _final_payload(entries)
    complete_units = _serialized_units(complete_payload)

    result = _admit(
        _input_payload(entries),
        context=complete_units - 1,
        memory_pool=complete_units,
    )
    assert _memory_payloads(result) == []
    assert result.messages == ()

    metadata = result.memory_admission
    assert metadata is not None
    assert metadata.selected_ids == tuple(item["id"] for item in entries)
    assert metadata.admitted_ids == ()
    assert set(metadata.dropped_reasons) == {item["id"] for item in entries}
    assert set(metadata.dropped_reasons.values()) == {"CONTEXT_WINDOW_BUDGET"}


@pytest.mark.parametrize(
    "category",
    [
        ContextCategory.SYSTEM_PROMPT,
        ContextCategory.CONVERSATION,
        ContextCategory.RAG_EVIDENCE,
        ContextCategory.TOOL_RESULT,
    ],
    ids=["system", "conversation", "rag", "tool_result"],
)
def test_same_memory_json_in_another_category_is_not_misidentified(
    category: ContextCategory,
) -> None:
    item = _entry(
        _MEMORY_ID,
        "FACT",
        "This is ordinary system text, despite its JSON shape.",
    )
    raw_payload = _input_payload([item])
    raw_content = _json(raw_payload)

    result = _admit(
        raw_payload,
        context=len(raw_content),
        memory_pool=10_000,
        category=category,
    )

    memory_payloads = [
        payload
        for message, message_category in zip(
            result.messages,
            result.message_categories,
            strict=True,
        )
        if message_category is ContextCategory.MEMORY
        for payload in [json.loads(message.content or "null")]
    ]
    assert memory_payloads == []
    assert result.memory_admission is None
    if category is ContextCategory.SYSTEM_PROMPT:
        assert result.messages[0].content == raw_content
        assert result.message_categories == (category,)


def _malformed_memory_payloads() -> list[tuple[str, dict[str, Any]]]:
    duplicate_id = UUID(_MEMORY_ID)
    valid_entry = _entry(_MEMORY_ID, "FACT", "A valid memory body.")
    return [
        ("memories_not_list", {"memories": {"id": _MEMORY_ID}}),
        (
            "entry_not_object",
            {"memories": ["id/kind/content collapsed into one string"]},
        ),
        (
            "wrong_id_type",
            _input_payload([{**valid_entry, "id": 7}]),
        ),
        (
            "invalid_uuid",
            _input_payload([{**valid_entry, "id": "not-a-uuid"}]),
        ),
        (
            "missing_id",
            _input_payload([{"kind": "FACT", "content": "Missing identity."}]),
        ),
        (
            "wrong_kind_type",
            _input_payload([{**valid_entry, "kind": ["FACT"]}]),
        ),
        (
            "wrong_content_type",
            _input_payload([{**valid_entry, "content": {"body": "collapsed"}}]),
        ),
        (
            "missing_content",
            _input_payload([{"id": _MEMORY_ID, "kind": "FACT"}]),
        ),
        (
            "wrong_root_trust_type",
            {"trust": ["SYSTEM"], "memories": []},
        ),
        (
            "duplicate_uuid",
            _input_payload([valid_entry, {**valid_entry}]),
        ),
        (
            "duplicate_uuid_other_representation",
            _input_payload(
                [
                    valid_entry,
                    {**valid_entry, "id": "{" + str(duplicate_id).upper() + "}"},
                ]
            ),
        ),
    ]


@pytest.mark.parametrize(
    ("payload",),
    [pytest.param(payload, id=case_id) for case_id, payload in _malformed_memory_payloads()],
)
@pytest.mark.parametrize("memory_pool", [0, 10_000], ids=["zero_budget", "ample_budget"])
def test_invalid_memory_fails_closed_at_any_pool_budget(
    payload: dict[str, Any],
    memory_pool: int,
) -> None:
    with pytest.raises(AgentHubError) as raised:
        _admit(
            payload,
            context=10_000,
            memory_pool=memory_pool,
        )

    assert raised.value.code == "AGENT_MEMORY_PAYLOAD_INVALID"
    assert raised.value.status_code == 422
