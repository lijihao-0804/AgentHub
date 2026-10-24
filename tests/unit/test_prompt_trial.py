import json
from copy import deepcopy

import pytest

from benchmarks.public_rag.consumer_replay import to_wire
from benchmarks.public_rag.evidence_plan_replay import ANSWER_PROMPT
from benchmarks.public_rag.prompt_trial import PROMPTS, delete_records, prompt_request
from packages.core.errors.exceptions import AgentHubError
from tests.unit.test_attributed_reader import _request


def messages():
    wire = to_wire(_request().messages)
    wire[1]["content"] = ANSWER_PROMPT
    return wire


def test_task_instruction_preserves_evidence_task_and_original_policy():
    original = messages()
    changed = to_wire(prompt_request(original, "task_contract").messages)
    assert original == messages()
    assert changed[1]["content"] == PROMPTS["task_contract"]
    assert all(a == b for i, (a, b) in enumerate(zip(original, changed, strict=True)) if i != 1)
    assert to_wire(prompt_request(original, "baseline").messages) == original


def test_evidence_deletion_preserves_remaining_text_lifecycle_and_refs():
    original = messages()
    changed = delete_records(original, ["chunk-0"])
    old = json.loads(original[-1]["content"])
    new = json.loads(changed[-1]["content"])
    assert original[:-1] == changed[:-1]
    assert new["data"]["results"] == old["data"]["results"][1:]
    assert new["data"]["evidence_refs"] == old["data"]["evidence_refs"][1:]
    for arm in PROMPTS:
        prompt_request(changed, arm)
    with pytest.raises(ValueError):
        delete_records(original, ["chunk-0", "chunk-1"])


def test_changed_or_duplicate_trusted_instruction_cannot_enter_trial():
    wire = messages()
    wire[1]["content"] = "Answer differently"
    with pytest.raises(ValueError):
        prompt_request(wire, "baseline")
    wire = messages()
    wire.insert(1, deepcopy(wire[1]))
    with pytest.raises(ValueError):
        prompt_request(wire, "task_contract")


def test_prompt_budget_cannot_silently_remove_original_sources():
    wire = messages()
    wire[0]["content"] += "x" * 17000
    with pytest.raises(AgentHubError) as caught:
        prompt_request(wire, "task_contract")
    assert caught.value.code == "AGENT_CONTEXT_BUDGET_EXCEEDED"
