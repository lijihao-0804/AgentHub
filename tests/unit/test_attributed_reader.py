import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from benchmarks.public_rag.consumer_replay import CONTEXT, _spec
from benchmarks.public_rag.experimental.attributed_reader import (
    ANSWER_SUFFIX,
    SPAN_PROMPT,
    AttributedEvidenceReader,
    validate_quotes,
    validate_reader_answer,
)
from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
)
from packages.model_gateway.contracts import (
    CapabilityRequirements,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    RetryPolicy,
)


def _record(index: int, snippet: str, *, score: float = 1.0) -> dict:
    return {
        "chunk_id": f"chunk-{index}",
        "document_id": f"doc-{index}",
        "document_revision_id": f"revision-{index}",
        "snapshot_id": "snapshot",
        "knowledge_base_id": "knowledge-base",
        "document_name": f"Title {index}",
        "effective_date": "2026-01-01",
        "locator": {"paragraph_ordinal": index, "start": 10, "end": 10 + len(snippet)},
        "rerank_score": score,
        "retrieval_score": score,
        "snippet": snippet,
        "source": f"document:doc-{index}/revision:revision-{index}",
        "superseded": index == 1,
    }


def _payload(records: list[dict]) -> dict:
    references = [
        {
            "chunk_id": record["chunk_id"],
            "document_revision_id": record["document_revision_id"],
            "snapshot_id": record["snapshot_id"],
            "knowledge_base_id": record["knowledge_base_id"],
        }
        for record in records
    ]
    return {
        "evidence_format": "rag_records_v1",
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": False,
        "data": {"results": records, "evidence_refs": references},
    }


def _request(
    records: list[dict] | None = None,
    *,
    required_capabilities: CapabilityRequirements | None = None,
) -> ModelRequest:
    records = records or [
        _record(0, "Ada was born in London."),
        _record(1, "London is the capital of England."),
    ]
    return ModelRequest(
        messages=(
            ModelMessage("system", "Runtime policy."),
            ModelMessage(
                "system",
                "Answer in the supplied JSON format.",
            ),
            ModelMessage("user", "Where was Ada born?"),
            ModelMessage(
                "assistant",
                tool_calls=(
                    ModelToolCall(
                        "search_knowledge",
                        {"query": "Where was Ada born?"},
                        "paired-call",
                    ),
                ),
            ),
            ModelMessage(
                "tool",
                json.dumps(
                    _payload(records),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                name="search_knowledge",
                tool_call_id="paired-call",
            ),
        ),
        required_capabilities=required_capabilities or CapabilityRequirements(),
    )


def _quote_json(source_id: str, quote: str, **extra) -> str:
    return json.dumps(
        {"quotes": [{"source_id": source_id, "quote": quote}], **extra},
        ensure_ascii=False,
    )


def _response(plan, content: str) -> ModelResponse:
    return ModelResponse(content, "test", plan.primary.model)


@pytest.mark.parametrize(
    "output",
    [
        _quote_json("chunk-0", "Ada was born in Paris."),
        _quote_json("unknown", "Ada was born in London."),
        json.dumps(
            {
                "quotes": [
                    {"source_id": "chunk-0", "quote": "Ada was born in London."},
                    {"source_id": "chunk-0", "quote": "Ada was born in London."},
                ]
            }
        ),
        _quote_json("chunk-0", "Ada was born in London.", answer="London"),
        _quote_json("chunk-0", "Ada"),
        json.dumps({"quotes": [{"source_id": "chunk-0", "quote": "x" * 601}]}),
    ],
)
def test_invalid_quote_outputs_are_rejected_without_repair(output: str) -> None:
    records = [_record(0, "Ada was born in London."), _record(1, "x" * 601)]
    with pytest.raises(ValueError):
        validate_quotes(output, records)


def test_quote_validation_rejects_total_over_budget() -> None:
    records = [_record(0, "a" * 501), _record(1, "b" * 501)]
    quotes = [
        {"source_id": f"chunk-{index}", "quote": character * 501}
        for index, character in enumerate(("a", "b"))
    ] + [
        {
            "source_id": f"chunk-{index}",
            "quote": character * 500,
        }
        for index, character in enumerate(("a", "b"))
    ]
    output = json.dumps({"quotes": quotes})
    with pytest.raises(ValueError, match="budget"):
        validate_quotes(output, records)


@pytest.mark.asyncio
async def test_prepare_and_read_preserve_sources_and_annotate_computed_offsets() -> None:
    request = _request(
        required_capabilities=CapabilityRequirements(
            required=frozenset({"tool_calling"}), max_context_tokens=16_384
        )
    )
    plan = _spec({}).model_plan
    source_id, quote = "chunk-1", "London is the capital"
    start = "London is the capital of England.".index(quote)
    attribution_response = _response(plan, _quote_json(source_id, quote))
    answer_response = _response(
        plan, '{"answer":"London","unanswerable":false,"cited_chunk_ids":["chunk-1"]}'
    )
    gateway = AsyncMock()
    gateway.generate_resolved.side_effect = (attribution_response, answer_response)
    reader = AttributedEvidenceReader(gateway, plan, plan)

    result = await reader.read(CONTEXT, request, instruction_index=1)

    assert gateway.generate_resolved.await_count == 2
    attribution_call, answer_call = gateway.generate_resolved.await_args_list
    assert attribution_call.args[:2] == (CONTEXT, plan)
    assert answer_call.args[:2] == (CONTEXT, plan)
    assert attribution_call.args[2].required_capabilities == request.required_capabilities
    assert answer_call.args[2].required_capabilities == request.required_capabilities
    assert attribution_call.args[2].messages[0] == request.messages[0]
    assert attribution_call.args[2].messages[1].content == SPAN_PROMPT
    assert attribution_call.args[2].messages[2] == request.messages[2]
    assert attribution_call.args[2].messages[3] == request.messages[3]
    assert attribution_call.args[2].messages[-1] == request.messages[-1]
    assert result.preparation.answer_request.messages[1].content.startswith(
        request.messages[1].content
    )
    assert result.preparation.answer_request.messages[1].content.endswith(ANSWER_SUFFIX)
    annotated = json.loads(result.preparation.answer_request.messages[-1].content)
    annotated_data = annotated["data"]
    original_data = json.loads(request.messages[-1].content)["data"]
    assert annotated_data["evidence_refs"] == original_data["evidence_refs"]
    for original_record, annotated_record in zip(
        original_data["results"], annotated_data["results"], strict=True
    ):
        assert {
            key: value for key, value in annotated_record.items() if key != "reading_quotes"
        } == original_record
    assert annotated_data["results"][0]["reading_quotes"] == []
    assert annotated_data["results"][1]["reading_quotes"] == [
        {"start": start, "end": start + len(quote), "text": quote}
    ]
    assert result.answer_response == answer_response


@pytest.mark.asyncio
async def test_final_answer_cannot_return_an_invisible_source_after_valid_span_selection():
    plan = _spec({}).model_plan
    gateway = AsyncMock()
    gateway.generate_resolved.side_effect = (
        _response(plan, _quote_json("chunk-0", "Ada was born in London.")),
        _response(
            plan,
            json.dumps(
                {"answer": "London", "unanswerable": False, "cited_chunk_ids": ["chunk-999"]}
            ),
        ),
    )
    with pytest.raises(ValueError, match="invisible"):
        await AttributedEvidenceReader(gateway, plan, plan).read(
            CONTEXT, _request(), instruction_index=1
        )
    assert gateway.generate_resolved.await_count == 2


@pytest.mark.parametrize(
    "value",
    [
        {"answer": "London", "unanswerable": False, "cited_chunk_ids": []},
        {"answer": "", "unanswerable": True, "cited_chunk_ids": ["chunk-0"]},
        {"answer": "London", "unanswerable": False, "cited_chunk_ids": ["chunk-0", "chunk-0"]},
    ],
)
def test_answer_abstention_and_citation_contract_is_checked_on_final_delivery(value):
    with pytest.raises(ValueError):
        validate_reader_answer(json.dumps(value), [_record(0, "Ada was born in London.")])


@pytest.mark.asyncio
async def test_invalid_attribution_never_calls_answer_model() -> None:
    plan = _spec({}).model_plan
    gateway = AsyncMock()
    gateway.generate_resolved.return_value = _response(
        plan, _quote_json("chunk-0", "Ada was born in Paris.")
    )
    reader = AttributedEvidenceReader(gateway, plan, plan)

    with pytest.raises(ValueError):
        await reader.read(CONTEXT, _request(), instruction_index=1)

    gateway.generate_resolved.assert_awaited_once()


@pytest.mark.asyncio
async def test_reading_quotes_that_push_original_record_over_budget_are_rejected() -> None:
    records = [_record(index, "x" * 1_000) for index in range(5)]
    request = _request(records)
    original_content = request.messages[-1].content
    assert 7_500 < len(original_content.encode("utf-8")) < 8_000
    assert (
        ContextBudgetPolicy(
            profiles=_spec({}).model_plan.chain,
            config=ContextBudgetConfig(
                reserved_output_tokens=512,
                max_retrieval_tokens=8000,
            ),
        )
        .admit(
            request.messages,
            categories=(
                ContextCategory.SYSTEM_PROMPT,
                ContextCategory.SYSTEM_PROMPT,
                ContextCategory.CURRENT_USER_TASK,
                ContextCategory.CONVERSATION,
                ContextCategory.RAG_EVIDENCE,
            ),
            exchange_groups=(None, None, None, "paired-call", "paired-call"),
        )
        .messages
        == request.messages
    )

    plan = _spec({}).model_plan
    roomy_primary = replace(
        plan.primary,
        capabilities=replace(plan.primary.capabilities, max_context_tokens=32_000),
    )
    attribution_plan = replace(
        plan,
        primary=replace(roomy_primary, max_tokens=512),
    )
    answer_plan = replace(
        plan,
        primary=replace(roomy_primary, max_tokens=256),
    )
    output = json.dumps(
        {"quotes": [{"source_id": f"chunk-{index}", "quote": "x" * 600} for index in range(4)]}
    )
    gateway = AsyncMock()
    gateway.generate_resolved.return_value = _response(attribution_plan, output)

    with pytest.raises(ValueError):
        await AttributedEvidenceReader(gateway, attribution_plan, answer_plan).prepare(
            CONTEXT, request, instruction_index=1
        )

    gateway.generate_resolved.assert_awaited_once()


@pytest.mark.parametrize(
    ("content", "fallback", "retry"),
    [
        (_quote_json("chunk-0", "Ada was born in London."), True, 1),
        (_quote_json("chunk-0", "Ada was born in London."), False, 2),
    ],
)
@pytest.mark.asyncio
async def test_model_fallback_retry_or_invalid_output_cannot_silently_advance(
    content: str, fallback: bool, retry: int
) -> None:
    plan = _spec({}).model_plan
    changed = replace(
        plan,
        fallbacks=(plan.primary,) if fallback else (),
        retry_policy=RetryPolicy(max_attempts=retry),
    )
    gateway = AsyncMock()
    gateway.generate_resolved.return_value = _response(plan, content)
    with pytest.raises(ValueError):
        reader = AttributedEvidenceReader(gateway, changed, plan)
        await reader.read(CONTEXT, _request(), instruction_index=1)
    gateway.generate_resolved.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_quote_selection_stops_after_attribution() -> None:
    plan = _spec({}).model_plan
    gateway = AsyncMock()
    gateway.generate_resolved.return_value = _response(plan, '{"quotes":[]}')
    with pytest.raises(ValueError, match="no source quotations"):
        await AttributedEvidenceReader(gateway, plan, plan).read(
            CONTEXT, _request(), instruction_index=1
        )
    gateway.generate_resolved.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrong_model_or_tool_call_output_stops_after_current_stage() -> None:
    plan = _spec({}).model_plan
    other_model = _response(plan, _quote_json("chunk-0", "Ada was born in London."))
    other_model = replace(other_model, model="another-model")
    gateway = AsyncMock()
    gateway.generate_resolved.return_value = other_model
    with pytest.raises(ValueError):
        await AttributedEvidenceReader(gateway, plan, plan).read(
            CONTEXT, _request(), instruction_index=1
        )
    gateway.generate_resolved.assert_awaited_once()

    gateway.generate_resolved.reset_mock()
    gateway.generate_resolved.return_value = replace(
        other_model,
        model=plan.primary.model,
        tool_calls=(ModelToolCall("unexpected_tool", {}, "unexpected-call"),),
    )
    with pytest.raises(ValueError):
        await AttributedEvidenceReader(gateway, plan, plan).read(
            CONTEXT, _request(), instruction_index=1
        )
    gateway.generate_resolved.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_stage_models_are_rejected_before_any_call() -> None:
    plan = _spec({}).model_plan
    changed = replace(
        plan,
        primary=replace(plan.primary, model="different-model"),
    )
    gateway = AsyncMock()
    with pytest.raises(ValueError, match="same fixed model"):
        AttributedEvidenceReader(gateway, plan, changed)
    gateway.generate_resolved.assert_not_awaited()
