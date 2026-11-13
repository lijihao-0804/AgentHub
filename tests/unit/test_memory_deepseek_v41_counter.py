"""The research counter is restricted and agrees with recorded provider usage."""

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.evaluation.memory_deepseek_v41_counter import MODEL, DeepseekV41TextCounter
from benchmarks.evaluation.memory_qmsum_protocol import QMSUM_COMMIT
from packages.model_gateway.contracts import (
    AliyunDeepSeekRequestMode,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ModelToolDefinition,
    StructuredOutputSchema,
)
from tests.support.qmsum_counter_assets import STUDY_EVIDENCE, resolve_frozen_inputs

pytest_plugins = ("tests.support.deepseek_assets",)


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
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        DeepseekV41TextCounter.from_file(Path(__file__))


def test_paired_calibration_matches_real_provider_input_usage(
    deepseek_v41_tokenizer_path: Path,
):
    counter = DeepseekV41TextCounter.from_file(deepseek_v41_tokenizer_path)
    messages = (
        ModelMessage(
            "user",
            "Calculate 987654321 times 123456789. Return only the final number.",
        ),
    )
    thinking_request = ModelRequest(messages)
    non_thinking_request = ModelRequest(
        messages,
        request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
    )
    assert counter.count(thinking_request, thinking=True) == 47
    assert counter.count(non_thinking_request, thinking=False) == 21


def test_four_frozen_summary_requests_match_recorded_provider_usage(
    deepseek_v41_tokenizer_path: Path,
):
    evidence = json.loads((STUDY_EVIDENCE / "qmsum-outcomes.json").read_bytes())
    identities = json.loads((STUDY_EVIDENCE / "qmsum-summary-input-digests.json").read_bytes())
    frozen = json.loads(resolve_frozen_inputs().read_bytes())
    assert frozen["format"] == "qmsum-frozen-summary-inputs-v1"
    assert evidence["source_commit"] == QMSUM_COMMIT
    assert evidence["model"] == frozen["model"] == MODEL
    assert evidence["price_identity"] == frozen["price_identity"]
    assert frozen["max_output_tokens"] == 2048
    observations = [row for row in evidence["cases"] if "summary_measurements" in row]
    assert len(frozen["cases"]) == len(observations) == len(identities["cases"]) == 4
    assert len({row["question_id"] for row in observations}) == 4
    counter = DeepseekV41TextCounter.from_file(deepseek_v41_tokenizer_path)
    for case, observed, identity in zip(
        frozen["cases"], observations, identities["cases"], strict=True
    ):
        question_id = case["question_id"]
        assert question_id == observed["question_id"] == identity["question_id"]
        canonical = json.dumps(
            case["messages"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        assert hashlib.sha256(canonical).hexdigest() == identity["messages_sha256"], question_id
        messages = tuple(ModelMessage(**message) for message in case["messages"])
        expected = observed["summary_measurements"]["input_tokens"]
        assert counter.count(ModelRequest(messages), thinking=True) == expected, question_id
