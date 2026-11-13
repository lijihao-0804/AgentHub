from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from packages.core.config.settings import Settings
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
    ModelToolDefinition,
    ModelUsage,
)
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ProviderCredential
from packages.model_gateway.profile_resolution import ResolvedModelProfile
from packages.observability.noop import NoopTraceSink
from tests.support.ae6b_provider import (
    AUDIT_PATH_ENV,
    CLARIFY_COMPLETE,
    CLARIFY_PROMPT,
    DENIED_COMPLETE,
    HIDDEN_LABEL_PREFIX,
    MARKER_CLARIFY,
    MARKER_PLAIN,
    MARKER_REFUSE,
    MARKER_UNKNOWN_USAGE,
    PLAIN_RESPONSE,
    REFUSE_FALLBACK_PHRASE,
    REFUSE_RESPONSE,
    AE6BDeterministicProviderAdapter,
    ae6b_model_gateway,
)

_WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
_PROFILE = ResolvedModelProfile(
    id=UUID("22222222-2222-4222-8222-222222222222"),
    workspace_id=_WORKSPACE_ID,
    provider_credential_id=UUID("33333333-3333-4333-8333-333333333333"),
    model="frozen-ae6b-model",
    temperature=Decimal("0.1"),
    max_tokens=512,
    timeout_seconds=Decimal("30"),
    fallback_profile_id=None,
    capabilities=ModelCapabilities(
        tool_calling=True,
        streaming=True,
        max_context_tokens=8_192,
    ),
)
_CREDENTIAL = ProviderCredential(
    id=_PROFILE.provider_credential_id,
    workspace_id=_WORKSPACE_ID,
    provider="frozen-provider",
    name="ae6b-deterministic",
    secret="not-a-real-secret",
)
_TOOLS = (
    ModelToolDefinition(
        name="query_customer",
        description="Read one customer.",
        parameters={
            "type": "object",
            "properties": {"customer_ref": {"type": "string"}},
            "required": ["customer_ref"],
            "additionalProperties": False,
        },
    ),
    ModelToolDefinition(
        name="create_ticket",
        description="Create one support ticket.",
        parameters={
            "type": "object",
            "properties": {
                "customer_ref": {"type": "string"},
                "subject": {"type": "string"},
                "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
            },
            "required": ["customer_ref", "subject"],
            "additionalProperties": False,
        },
    ),
)
_CUSTOMER_REF = "AE6B-0123456789abcdef0123456789abcdef"


def _request(*messages: ModelMessage, tools: tuple[ModelToolDefinition, ...] = _TOOLS):
    return ModelRequest(messages=messages, tools=tools)


def _audit_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _stream(
    adapter: AE6BDeterministicProviderAdapter,
    request: ModelRequest,
) -> list[ModelStreamEvent]:
    return [event async for event in adapter.stream(_PROFILE, _CREDENTIAL, request)]


@pytest.mark.asyncio
async def test_current_turn_only_clarify_records_safe_progress(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "provider.jsonl"
    adapter = AE6BDeterministicProviderAdapter(audit_path=audit_path)
    task = f"{MARKER_CLARIFY} Resolve private task text without disclosure."
    user_task = ModelMessage(role="user", content=task)

    first = await adapter.complete(
        _PROFILE,
        _CREDENTIAL,
        _request(user_task),
    )
    assert first.content == CLARIFY_PROMPT
    assert first.tool_calls == ()

    customer_turn = ModelMessage(role="user", content=_CUSTOMER_REF)
    second = await adapter.complete(
        _PROFILE,
        _CREDENTIAL,
        _request(customer_turn),
    )
    assert second.content == ""
    assert len(second.tool_calls) == 1
    assert second.tool_calls[0].name == "query_customer"
    assert second.tool_calls[0].arguments == {"customer_ref": _CUSTOMER_REF}

    query_call = second.tool_calls[0]
    proposed_query = ModelMessage(role="assistant", content="", tool_calls=(query_call,))
    query_result = ModelMessage(
        role="tool",
        name="query_customer",
        tool_call_id="ae6b-query-customer",
        content=json.dumps(
            {
                "status": "SUCCESS",
                "trust": "UNTRUSTED",
                "data": {"customer_found": True},
            },
            sort_keys=True,
        ),
    )
    third = await adapter.complete(
        _PROFILE,
        _CREDENTIAL,
        _request(
            customer_turn,
            proposed_query,
            query_result,
        ),
    )
    assert third.content == ""
    assert len(third.tool_calls) == 1
    assert third.tool_calls[0].name == "create_ticket"
    assert third.tool_calls[0].arguments == {
        "customer_ref": _CUSTOMER_REF,
        "subject": "Controlled AE6b ticket",
        "priority": "HIGH",
    }

    ticket_call = third.tool_calls[0]
    proposed_ticket = ModelMessage(role="assistant", content="", tool_calls=(ticket_call,))
    ticket_result = ModelMessage(
        role="tool",
        name="create_ticket",
        tool_call_id="ae6b-create-ticket",
        content=json.dumps(
            {
                "status": "SUCCESS",
                "trust": "UNTRUSTED",
                "data": {"created": True},
            },
            sort_keys=True,
        ),
    )
    fourth = await adapter.complete(
        _PROFILE,
        _CREDENTIAL,
        _request(
            customer_turn,
            proposed_query,
            query_result,
            proposed_ticket,
            ticket_result,
        ),
    )
    assert fourth.content == CLARIFY_COMPLETE
    assert fourth.tool_calls == ()

    for response in (first, second, third, fourth):
        assert response.provider == _CREDENTIAL.provider
        assert response.model == _PROFILE.model
        assert response.usage == ModelUsage(
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
            cached_tokens=0,
        )
        assert response.cost_estimate is None

    records = _audit_records(audit_path)
    assert len(records) == 4
    assert len({record["request_hash"] for record in records}) == 4
    assert {record["marker"] for record in records} == {MARKER_CLARIFY}
    assert {record["provider"] for record in records} == {_CREDENTIAL.provider}
    assert {record["model"] for record in records} == {_PROFILE.model}
    assert {record["pid"] for record in records} == {os.getpid()}
    assert {record["input_tokens"] for record in records} == {11}
    assert {record["output_tokens"] for record in records} == {7}
    assert {record["total_tokens"] for record in records} == {18}
    assert {record["cached_tokens"] for record in records} == {0}
    assert [record["user_turn_count"] for record in records] == [1, 1, 1, 1]
    assert [record["user_ref_present"] for record in records] == [
        False,
        True,
        True,
        True,
    ]
    assert [record["user_fixture_ref_present"] for record in records] == [
        False,
        True,
        True,
        True,
    ]
    assert all(record["fixture_ref_outside_user_before_disclosure"] is False for record in records)
    assert all(record["expected_label_present"] is False for record in records)
    assert all(
        set(record)
        == {
            "request_hash",
            "marker",
            "provider",
            "model",
            "pid",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "user_turn_count",
            "user_ref_present",
            "user_fixture_ref_present",
            "fixture_ref_outside_user_before_disclosure",
            "expected_label_present",
        }
        for record in records
    )
    raw_audit = audit_path.read_text(encoding="utf-8")
    assert "private task text" not in raw_audit
    assert _CUSTOMER_REF not in raw_audit
    assert "Controlled AE6b ticket" not in raw_audit


@pytest.mark.asyncio
async def test_hidden_label_is_rejected_before_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "provider.jsonl"
    adapter = AE6BDeterministicProviderAdapter(audit_path=audit_path)
    request = _request(
        ModelMessage(
            role="user",
            content=f"{MARKER_PLAIN} {HIDDEN_LABEL_PREFIX}do-not-disclose",
        )
    )

    with pytest.raises(ValueError, match="AE6B_HIDDEN_LABEL_LEAK"):
        await adapter.complete(_PROFILE, _CREDENTIAL, request)

    assert not audit_path.exists()


@pytest.mark.asyncio
async def test_audit_flags_fixture_reference_before_user_disclosure(tmp_path: Path) -> None:
    audit_path = tmp_path / "provider.jsonl"
    adapter = AE6BDeterministicProviderAdapter(audit_path=audit_path)
    request = _request(
        ModelMessage(role="system", content=f"Leaked fixture {_CUSTOMER_REF}"),
        ModelMessage(role="user", content=f"{MARKER_CLARIFY} Need support."),
    )

    response = await adapter.complete(_PROFILE, _CREDENTIAL, request)

    assert response.content == CLARIFY_PROMPT
    record = _audit_records(audit_path)[0]
    assert record["user_turn_count"] == 1
    assert record["user_ref_present"] is False
    assert record["user_fixture_ref_present"] is False
    assert record["fixture_ref_outside_user_before_disclosure"] is True
    assert record["expected_label_present"] is False
    assert _CUSTOMER_REF not in audit_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_refuse_and_denied_markers_do_not_write_again(tmp_path: Path) -> None:
    adapter = AE6BDeterministicProviderAdapter(audit_path=tmp_path / "provider.jsonl")

    refuse_task = ModelMessage(role="user", content=f"{MARKER_REFUSE} Resolve this.")
    first = await adapter.complete(_PROFILE, _CREDENTIAL, _request(refuse_task))
    assert first.content == CLARIFY_PROMPT
    refused = ModelMessage(
        role="user",
        content=f"{MARKER_REFUSE} {REFUSE_FALLBACK_PHRASE}",
    )
    second = await adapter.complete(
        _PROFILE,
        _CREDENTIAL,
        _request(refused),
    )
    assert second.content == REFUSE_RESPONSE
    assert second.tool_calls == ()

    customer = ModelMessage(role="user", content=_CUSTOMER_REF)
    query = ModelToolCall(
        name="query_customer",
        arguments={"customer_ref": _CUSTOMER_REF},
        provider_tool_call_id="ae6b-query-customer",
    )
    query_result = ModelMessage(
        role="tool",
        name="query_customer",
        tool_call_id="ae6b-query-customer",
        content='{"status":"SUCCESS","trust":"UNTRUSTED","data":{"found":true}}',
    )
    proposed_query = ModelMessage(role="assistant", content="", tool_calls=(query,))
    proposal = await adapter.complete(
        _PROFILE,
        _CREDENTIAL,
        _request(
            customer,
            proposed_query,
            query_result,
        ),
    )
    assert [call.name for call in proposal.tool_calls] == ["create_ticket"]

    ticket = proposal.tool_calls[0]
    denied_result = ModelMessage(
        role="tool",
        name="create_ticket",
        tool_call_id="ae6b-create-ticket",
        content=(
            '{"status":"ERROR","trust":"UNTRUSTED",'
            '"error_code":"TOOL_APPROVAL_DENIED","message":"The action was not approved."}'
        ),
    )
    final = await adapter.complete(
        _PROFILE,
        _CREDENTIAL,
        _request(
            customer,
            proposed_query,
            query_result,
            ModelMessage(role="assistant", content="", tool_calls=(ticket,)),
            denied_result,
        ),
    )
    assert final.content == DENIED_COMPLETE
    assert final.tool_calls == ()


@pytest.mark.asyncio
async def test_plain_and_unknown_usage_markers_keep_usage_semantics(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "provider.jsonl"
    adapter = AE6BDeterministicProviderAdapter(audit_path=audit_path)

    plain = await adapter.complete(
        _PROFILE,
        _CREDENTIAL,
        _request(ModelMessage(role="user", content=MARKER_PLAIN)),
    )
    unknown = await adapter.complete(
        _PROFILE,
        _CREDENTIAL,
        _request(ModelMessage(role="user", content=MARKER_UNKNOWN_USAGE)),
    )

    assert plain.content == unknown.content == PLAIN_RESPONSE
    assert plain.usage == ModelUsage(11, 7, 18, 0)
    assert unknown.usage is None
    assert plain.cost_estimate is unknown.cost_estimate is None
    records = _audit_records(audit_path)
    assert records[-1]["marker"] == MARKER_UNKNOWN_USAGE
    assert records[-1]["input_tokens"] is None
    assert records[-1]["output_tokens"] is None
    assert records[-1]["total_tokens"] is None
    assert records[-1]["cached_tokens"] is None


@pytest.mark.asyncio
async def test_stream_and_health_complete_the_adapter_protocol(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "provider.jsonl"
    adapter = AE6BDeterministicProviderAdapter(audit_path=audit_path)
    plain_request = _request(ModelMessage(role="user", content=MARKER_PLAIN))

    events = await _stream(adapter, plain_request)
    assert [event.event_type for event in events] == [
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.USAGE,
        ModelStreamEventType.COMPLETED,
    ]
    assert events[0].message_delta == PLAIN_RESPONSE
    assert events[1].usage == ModelUsage(11, 7, 18, 0)
    assert events[2].response is not None
    assert events[2].response.provider == _CREDENTIAL.provider
    assert events[2].response.model == _PROFILE.model

    query_request = _request(
        ModelMessage(role="user", content=f"{MARKER_CLARIFY} Need support."),
        ModelMessage(role="user", content=_CUSTOMER_REF),
    )
    tool_events = await _stream(adapter, query_request)
    assert [event.event_type for event in tool_events] == [
        ModelStreamEventType.TOOL_CALL_DELTA,
        ModelStreamEventType.USAGE,
        ModelStreamEventType.COMPLETED,
    ]
    assert tool_events[0].tool_call_delta is not None
    assert tool_events[0].tool_call_delta.name == "query_customer"

    assert await adapter.health(_PROFILE, _CREDENTIAL) is None
    assert len(_audit_records(audit_path)) == 2


@pytest.mark.asyncio
async def test_model_gateway_factory_keeps_real_gateway_and_provider_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "gateway.jsonl"
    monkeypatch.setenv(AUDIT_PATH_ENV, str(audit_path))
    trace_sink = NoopTraceSink()
    gateway = ae6b_model_gateway(
        object(),
        settings=Settings(testing=True, environment="testing"),
        trace_sink=trace_sink,
    )

    assert isinstance(gateway, SqlAlchemyModelGateway)
    assert isinstance(gateway.adapter, AE6BDeterministicProviderAdapter)
    assert gateway.trace_sink is trace_sink
    response = await gateway.adapter.complete(
        _PROFILE,
        _CREDENTIAL,
        _request(ModelMessage(role="user", content=MARKER_PLAIN)),
    )
    assert response.provider == "frozen-provider"
    assert response.model == "frozen-ae6b-model"
    assert len(_audit_records(audit_path)) == 1
