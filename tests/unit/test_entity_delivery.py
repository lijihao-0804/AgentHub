import json
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest

from benchmarks.public_rag.experimental.entity_delivery import (
    EntityDeliveryReader,
    EntityTask,
    build_request,
    decode_delivery,
)
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelResponse,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
)


def task():
    return EntityTask(
        "Which dataset trains the model?", "p1", "Paper", "revision-1", "TRAINING_DATA"
    )


def views():
    return [
        {
            "source_id": "s1",
            "paper_id": "p1",
            "source_revision": "revision-1",
            "text": "Our model trains on SQuAD. RoBERTa is related work.",
        }
    ]


def typed():
    return {
        "status": "ANSWERED",
        "entities": [
            {
                "value": "SQuAD",
                "supports": [{"source_id": "s1", "quote": "Our model trains on SQuAD."}],
            }
        ],
        "explanation": "",
        "missing": [],
        "clarification": None,
    }


def test_model_input_has_task_and_original_views_without_offline_annotations():
    records = [{**views()[0], "gold_entities": ["SECRET"], "review_status": "SECRET"}]
    request = build_request(task(), records, "typed")
    assert all("SECRET" not in message.content for message in request.messages)
    payload = json.loads(request.messages[-1].content)
    assert payload["records"] == [{"source_id": "s1", "text": views()[0]["text"]}]
    assert json.loads(request.messages[1].content)["requested_relation"] == "TRAINING_DATA"


def test_quote_must_belong_to_current_visible_source_and_contain_whole_entity():
    assert decode_delivery(json.dumps(typed()), task(), views(), "typed")["answer_values"] == [
        "SQuAD"
    ]
    output = typed()
    output["entities"][0]["supports"][0]["quote"] = "Our model trains on SQUAD."
    with pytest.raises(ValueError, match="exact visible"):
        decode_delivery(json.dumps(output), task(), views(), "typed")
    output = typed()
    output["entities"][0] = {
        "value": "BERT",
        "supports": [{"source_id": "s1", "quote": "RoBERTa is related work."}],
    }
    with pytest.raises(ValueError, match="name is absent"):
        decode_delivery(json.dumps(output), task(), views(), "typed")
    with pytest.raises(ValueError, match="another paper or revision"):
        build_request(replace(task(), source_revision="revision-2"), views(), "typed")


def test_partial_delivery_cannot_claim_complete_or_drop_its_missing_fact():
    output = typed()
    output.update(status="PARTIAL", missing=["The second training corpus name"])
    result = decode_delivery(json.dumps(output), task(), views(), "typed")
    assert result["delivery_status"] == "PARTIAL"
    output["status"] = "ANSWERED"
    with pytest.raises(ValueError, match="contradict"):
        decode_delivery(json.dumps(output), task(), views(), "typed")
    output.update(status="PARTIAL", missing=[])
    with pytest.raises(ValueError, match="contradict"):
        decode_delivery(json.dumps(output), task(), views(), "typed")


@pytest.mark.asyncio
async def test_service_uses_frozen_plan_and_aborts_oversized_evidence_before_call():
    baseline = {
        "answer_values": ["SQuAD"],
        "explanation": "",
        "unanswerable": False,
        "cited_source_ids": ["s1"],
    }

    class Gateway:
        def __init__(self):
            self.calls = []

        async def generate_resolved(self, context, plan, request):
            self.calls.append((context, plan, request))
            return ModelResponse(provider="test", model="test", content=json.dumps(baseline))

    plan = ResolvedModelExecutionPlan(
        primary=ResolvedModelExecutionProfile(
            id=UUID(int=1),
            workspace_id=UUID(int=1),
            provider_credential_id=UUID(int=1),
            provider="test",
            model="test",
            temperature=Decimal(0),
            max_tokens=1024,
            timeout_seconds=Decimal(20),
            capabilities=ModelCapabilities(max_context_tokens=32768),
        )
    )
    gateway = Gateway()
    reader = EntityDeliveryReader(gateway)
    result = await reader.read(None, plan, task(), views(), "baseline")
    assert result.delivery["answer_values"] == ["SQuAD"]
    assert gateway.calls[0][1] is plan
    too_large = [{**views()[0], "text": "original evidence. " * 1000}]
    with pytest.raises(ValueError, match="context admission"):
        await reader.read(None, plan, task(), too_large, "typed")
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_configured_gateway_converts_resolved_profile_to_frozen_provider_identity():
    from benchmarks.public_rag.consumer_replay import CONTEXT
    from benchmarks.public_rag.entity_delivery_live import model_plan
    from packages.model_gateway.configured_route import ConfiguredRoute, Endpoint, build_gateway

    class Adapter:
        def __init__(self):
            self.calls = 0

        async def complete(self, profile, credential, request):
            self.calls += 1
            return ModelResponse(
                provider=credential.provider, model=profile.model, content=json.dumps(typed())
            )

    adapter = Adapter()
    route = ConfiguredRoute(
        1,
        "local-rag",
        Endpoint(
            name="unit",
            provider="openai-compatible",
            model="unit",
            base_url="http://127.0.0.1:3425/v1",
            api_key="unused",
            max_tokens=1024,
            capabilities={
                "max_context_tokens": 32768,
                "tool_calling": False,
                "streaming": False,
                "structured_output": False,
                "vision": False,
            },
        ),
        (),
    )
    gateway, profile_id = build_gateway(route, CONTEXT, adapter=adapter)
    plan = await model_plan(gateway, profile_id)
    assert plan.primary.provider == "openai-compatible"
    result = await EntityDeliveryReader(gateway).read(CONTEXT, plan, task(), views(), "typed")
    assert result.delivery["answer_values"] == ["SQuAD"]
    assert adapter.calls == 1
