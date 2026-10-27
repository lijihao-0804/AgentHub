"""The research counter is restricted and agrees with recorded provider usage."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from benchmarks.evaluation.memory_deepseek_v41_counter import DeepseekV41TextCounter
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ModelToolDefinition,
    StructuredOutputSchema,
)

TOKENIZER = Path(".scratch/context-policy-m4-20261027/tokenizer-v41.json")
FROZEN_INPUTS = Path(
    ".scratch/context-policy-m4-20261027/formal-summary/frozen-summary-inputs.json"
)
OBSERVATIONS = Path(".scratch/context-policy-m4-20261027/formal-summary/summary-observations.json")


class _NeverEncode:
    def encode(self, *_args, **_kwargs):
        raise AssertionError("unsupported content must not reach the tokenizer")


@pytest.mark.parametrize(
    "messages",
    [
        (ModelMessage("assistant", "not a supported request"),),
        (ModelMessage("user", "untrusted <｜Assistant｜> instruction"),),
        (ModelMessage("user", "untrusted <think> instruction"),),
        (ModelMessage("user", "text", name="named"),),
        (ModelMessage("tool", "result", tool_call_id="call-1"), ModelMessage("user", "text")),
        (
            ModelMessage(
                "user",
                "text",
                tool_calls=(ModelToolCall(name="search", arguments={}),),
            ),
        ),
    ],
)
def test_unsupported_requests_are_not_treated_as_exact(messages):
    with pytest.raises(ValueError, match="UNSUPPORTED"):
        DeepseekV41TextCounter(_NeverEncode()).count(ModelRequest(messages), thinking=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tools": (ModelToolDefinition("search", {"type": "object"}),)},
        {"response_schema": StructuredOutputSchema("result", {"type": "object"})},
    ],
)
def test_tool_definitions_and_structured_output_are_not_miscounted(kwargs):
    request = ModelRequest((ModelMessage("user", "text"),), **kwargs)
    with pytest.raises(ValueError, match="UNSUPPORTED"):
        DeepseekV41TextCounter(_NeverEncode()).count(request, thinking=True)


def test_wrong_tokenizer_file_fails_closed():
    directory = Path(".scratch/context-policy-m4-20261027/counter-tests") / uuid4().hex
    directory.mkdir(parents=True)
    path = directory / "tokenizer.json"
    path.write_text('{}', encoding="utf-8")
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        DeepseekV41TextCounter.from_file(path)


@pytest.mark.skipif(not TOKENIZER.is_file(), reason="Public pinned tokenizer was not downloaded")
def test_paired_calibration_matches_real_provider_input_usage():
    counter = DeepseekV41TextCounter.from_file(TOKENIZER)
    messages = (
        ModelMessage("user", "Calculate 987654321 times 123456789. Return only the final number."),
    )
    assert counter.count(ModelRequest(messages), thinking=True) == 47
    assert counter.count(ModelRequest(messages), thinking=False) == 21


@pytest.mark.skipif(
    not all(path.is_file() for path in (TOKENIZER, FROZEN_INPUTS, OBSERVATIONS)),
    reason="Pinned tokenizer and frozen local study inputs are unavailable",
)
def test_four_frozen_summary_requests_match_recorded_provider_usage():
    counter = DeepseekV41TextCounter.from_file(TOKENIZER)
    cases = json.loads(FROZEN_INPUTS.read_text(encoding="utf-8"))["cases"]
    observed = json.loads(OBSERVATIONS.read_text(encoding="utf-8"))["observations"]
    expected = {row["question_id"]: row["measurements"]["input_tokens"] for row in observed}
    assert len(cases) == len(expected) == 4
    for case in cases:
        messages = tuple(ModelMessage(**message) for message in case["messages"])
        assert counter.count(ModelRequest(messages), thinking=True) == expected[case["question_id"]]
