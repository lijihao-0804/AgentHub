"""Explicit lexical memory selection without changing legacy behavior or replay."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from apps.api.schemas.agents import MemoryConfigRequest
from packages.agent_runtime.frozen import (
    SUPPORTED_SPEC_SCHEMA_VERSIONS,
    parse_frozen_agent_spec,
)
from packages.agent_runtime.publish import _resolved_runtime_config, _validate_runtime_config
from packages.agent_runtime.runtime import _AgentRunGraph
from packages.core.errors.exceptions import AgentHubError
from packages.memory.contracts import SelectedMemory, memory_content_hash
from packages.memory.selection_policy import (
    LEXICAL_OVERLAP_POLICY_V1,
    MEMORY_SELECTION_LIMITATION_CODES,
    InvalidMemorySelectionPolicy,
    apply_memory_selection_policy,
)
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ResolvedModelExecutionProfile,
)


def _memory(content: str) -> SelectedMemory:
    return SelectedMemory(
        id=uuid4(),
        kind="FACT",
        content=content,
        salience=1,
        content_hash=memory_content_hash(content),
    )


def _profile(*, context: int = 32_768) -> ResolvedModelExecutionProfile:
    return ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        provider_credential_id=uuid4(),
        provider="test",
        model="test-model",
        temperature=Decimal(0),
        max_tokens=16,
        timeout_seconds=Decimal(30),
        capabilities=ModelCapabilities(max_context_tokens=context),
    )


def _resolved_model() -> tuple[tuple[ResolvedModelExecutionProfile, ...], dict[UUID, str]]:
    profile = _profile()
    return (profile,), {profile.id: "offline"}


def _frozen_spec(schema_version: int, memory: dict[str, object]) -> dict[str, object]:
    return {
        "spec_schema_version": schema_version,
        "model": {
            "profile_id": str(uuid4()),
            "credential_ref": str(uuid4()),
            "provider": "test",
            "model": "test-model",
            "temperature": 0,
            "max_tokens": 16,
            "timeout_seconds": 30,
            "capabilities": {"max_context_tokens": 32_768},
            "retry_policy": {"max_attempts": 1},
            "fallback_profiles": [],
        },
        "prompt": {"system_prompt": "Use approved context.", "prompt_version": 1},
        "retrieval": {"knowledge_bindings": []},
        "tools": [],
        "runtime": {"memory": memory},
    }


def test_explicit_selection_policy_contract_is_strict_and_opt_in() -> None:
    legacy = MemoryConfigRequest.model_validate({"long_term_memory": True})
    assert "selection_policy" not in legacy.model_dump(exclude_none=True)

    request = MemoryConfigRequest.model_validate(
        {"long_term_memory": True, "selection_policy": LEXICAL_OVERLAP_POLICY_V1}
    )
    assert request.selection_policy == LEXICAL_OVERLAP_POLICY_V1

    for invalid in (
        {"long_term_memory": False, "selection_policy": LEXICAL_OVERLAP_POLICY_V1},
        {"long_term_memory": True, "selection_policy": "semantic_v1"},
    ):
        with pytest.raises(ValidationError):
            MemoryConfigRequest.model_validate(invalid)


def test_publish_resolution_keeps_legacy_absent_and_freezes_explicit_policy() -> None:
    model = _resolved_model()
    legacy = _resolved_runtime_config({}, model)
    assert "memory" not in legacy

    validated = _validate_runtime_config(
        {
            "memory": {
                "long_term_memory": True,
                "selection_policy": LEXICAL_OVERLAP_POLICY_V1,
            }
        }
    )
    assert validated["memory"]["selection_policy"] == LEXICAL_OVERLAP_POLICY_V1

    resolved = _resolved_runtime_config(
        {
            "memory": {
                "long_term_memory": True,
                "selection_policy": LEXICAL_OVERLAP_POLICY_V1,
            }
        },
        model,
    )
    assert resolved["memory"]["selection_policy"] == LEXICAL_OVERLAP_POLICY_V1
    assert resolved["memory"]["long_term_memory"] is True

    with pytest.raises(AgentHubError) as invalid:
        _validate_runtime_config({"memory": {"selection_policy": LEXICAL_OVERLAP_POLICY_V1}})
    assert invalid.value.code == "INVALID_AGENT_CONFIG"

    with pytest.raises(AgentHubError):
        _validate_runtime_config(
            {
                "memory": {
                    "long_term_memory": True,
                    "selection_policy": "semantic_v1",
                }
            }
        )


def test_schema_two_recognizes_policy_without_a_global_schema_bump() -> None:
    assert SUPPORTED_SPEC_SCHEMA_VERSIONS == frozenset({1, 2, 3, 4, 5})
    parsed = parse_frozen_agent_spec(
        _frozen_spec(
            2,
            {
                "long_term_memory": True,
                "selection_policy": LEXICAL_OVERLAP_POLICY_V1,
            },
        ),
        workspace_id=uuid4(),
    )
    assert parsed.runtime["memory"]["selection_policy"] == LEXICAL_OVERLAP_POLICY_V1

    with pytest.raises(AgentHubError):
        parse_frozen_agent_spec(
            _frozen_spec(
                2,
                {"long_term_memory": True, "selection_policy": "semantic_v1"},
            ),
            workspace_id=uuid4(),
        )


def test_lexical_filter_reuses_shared_terms_and_fails_closed_on_empty_query() -> None:
    matching = _memory("The REFUND policy requires manager approval.")
    unrelated = _memory("The customer asked how to request money back.")
    chinese_matching = _memory("上次预算已经批准。")
    chinese_unrelated = _memory("The customer asked how to request money back.")

    selected = apply_memory_selection_policy(
        (matching, unrelated),
        query="refund policy",
        policy=LEXICAL_OVERLAP_POLICY_V1,
    )
    assert selected == (matching,)
    assert apply_memory_selection_policy(
        (chinese_matching, chinese_unrelated),
        query="预算是多少",
        policy=LEXICAL_OVERLAP_POLICY_V1,
    ) == (chinese_matching,)
    assert (
        apply_memory_selection_policy(
            (matching, unrelated), query="", policy=LEXICAL_OVERLAP_POLICY_V1
        )
        == ()
    )
    assert apply_memory_selection_policy(
        (matching, unrelated), query="refund policy", policy=None
    ) == (matching, unrelated)
    with pytest.raises(InvalidMemorySelectionPolicy):
        apply_memory_selection_policy((matching,), query="refund", policy="unknown_policy")


class _RecordingSelector:
    def __init__(self, memories: tuple[SelectedMemory, ...]) -> None:
        self.memories = memories
        self.select_calls = 0
        self.load_calls: list[tuple[UUID, ...]] = []

    async def select(self, *, workspace_id, agent_id, query, limit):  # noqa: ANN001
        del workspace_id, agent_id, query
        self.select_calls += 1
        return self.memories[:limit]

    async def load(self, *, workspace_id, memory_ids, memory_content_hashes=None):  # noqa: ANN001
        del workspace_id, memory_content_hashes
        self.load_calls.append(tuple(memory_ids))
        by_id = {item.id: item for item in self.memories}
        return tuple(by_id[memory_id] for memory_id in memory_ids if memory_id in by_id)


class _FakeSession:
    async def execute(self, statement):  # noqa: ANN001
        del statement

    async def commit(self) -> None:
        return None


def _graph(
    selector: _RecordingSelector,
    *,
    input_text: str,
    snapshot: dict | None = None,
):
    @asynccontextmanager
    async def session_factory():
        yield _FakeSession()

    service = SimpleNamespace(
        memory_selector=selector,
        session_factory=session_factory,
    )
    run = SimpleNamespace(
        id=uuid4(),
        input_text=input_text,
        effective_memory_snapshot=snapshot if snapshot is not None else {},
    )
    graph = _AgentRunGraph.__new__(_AgentRunGraph)
    graph.service = service
    graph.run = run
    return graph, run


async def _run_memory(graph, memory: dict[str, object]):
    return await _AgentRunGraph._memories(
        graph,
        SimpleNamespace(runtime={"memory": memory}),
        workspace_id=uuid4(),
        agent_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_legacy_runtime_keeps_every_selector_result_and_metadata() -> None:
    matching = _memory("Release policy for APAC.")
    unrelated = _memory("The team prefers concise answers.")
    selector = _RecordingSelector((matching, unrelated))
    graph, run = _graph(selector, input_text="release policy")

    messages, metadata = await _run_memory(graph, {"long_term_memory": True})

    assert selector.select_calls == 1
    assert len(json.loads(messages[0].content)["memories"]) == 2
    assert metadata == {"memory_count": 2, "replayed_from_snapshot": False}
    assert run.effective_memory_snapshot["memory_ids"] == [
        str(matching.id),
        str(unrelated.id),
    ]


@pytest.mark.asyncio
async def test_explicit_policy_filters_after_select_before_freeze() -> None:
    matching = _memory("Release policy for APAC.")
    unrelated = _memory("The team prefers concise answers.")
    selector = _RecordingSelector((matching, unrelated))
    graph, run = _graph(selector, input_text="release policy")

    messages, metadata = await _run_memory(
        graph,
        {
            "long_term_memory": True,
            "selection_policy": LEXICAL_OVERLAP_POLICY_V1,
        },
    )

    assert selector.select_calls == 1
    assert len(json.loads(messages[0].content)["memories"]) == 1
    assert run.effective_memory_snapshot["memory_ids"] == [str(matching.id)]
    assert metadata is not None
    assert metadata["memory_count"] == 1
    assert metadata["memory_selection"] == {
        "policy": LEXICAL_OVERLAP_POLICY_V1,
        "source": "LIVE_SELECTION",
        "filter_applied": True,
        "semantic_relevance": False,
        "limitations": list(MEMORY_SELECTION_LIMITATION_CODES),
        "candidate_count": 2,
        "selected_count": 1,
    }


@pytest.mark.asyncio
async def test_empty_query_freezes_an_empty_selection() -> None:
    selector = _RecordingSelector((_memory("Release policy for APAC."),))
    graph, run = _graph(selector, input_text="")

    messages, metadata = await _run_memory(
        graph,
        {
            "long_term_memory": True,
            "selection_policy": LEXICAL_OVERLAP_POLICY_V1,
        },
    )

    assert messages == []
    assert run.effective_memory_snapshot["memory_ids"] == []
    assert metadata is not None and metadata["memory_count"] == 0
    assert metadata["memory_selection"]["candidate_count"] == 1


@pytest.mark.asyncio
async def test_historical_snapshot_replay_never_applies_the_new_filter() -> None:
    matching = _memory("Release policy for APAC.")
    historically_selected = _memory("The team prefers concise answers.")
    selector = _RecordingSelector((matching, historically_selected))
    snapshot = {
        "selected_at": "2026-11-05T00:00:00+00:00",
        "memory_ids": [str(matching.id), str(historically_selected.id)],
        "memory_content_hashes": {
            str(matching.id): memory_content_hash(matching.content),
            str(historically_selected.id): memory_content_hash(historically_selected.content),
        },
    }
    graph, _ = _graph(selector, input_text="release policy", snapshot=snapshot)

    messages, metadata = await _run_memory(
        graph,
        {
            "long_term_memory": True,
            "selection_policy": LEXICAL_OVERLAP_POLICY_V1,
        },
    )

    assert selector.select_calls == 0
    assert selector.load_calls == [(matching.id, historically_selected.id)]
    assert len(json.loads(messages[0].content)["memories"]) == 2
    assert metadata is not None
    assert metadata["memory_selection"]["source"] == "FROZEN_SNAPSHOT"
    assert metadata["memory_selection"]["filter_applied"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["semantic_v1", []])
async def test_runtime_rejects_unknown_policy_before_selection(policy: object) -> None:
    selector = _RecordingSelector((_memory("Release policy for APAC."),))
    graph, _ = _graph(selector, input_text="release policy")

    with pytest.raises(AgentHubError) as raised:
        await _run_memory(
            graph,
            {"long_term_memory": True, "selection_policy": policy},
        )

    assert raised.value.code == "INVALID_MEMORY_SELECTION_POLICY"
    assert selector.select_calls == 0
