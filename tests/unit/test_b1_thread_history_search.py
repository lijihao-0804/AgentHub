"""Phase 1: the per-agent memory switch and the tool it turns on.

Three things have to hold at once for this feature to be safe to ship:

1. An agent that does not ask for memory publishes exactly the bytes it
   published before memory existed. Otherwise every agent in the workspace
   gets a new version hash for a feature nobody enabled.
2. The switch survives the freeze. A flag that is validated at publish time
   and then dropped at execution time is a setting that lies.
3. The tool only appears when all of its preconditions hold, and it reads
   only the thread it belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.memory_tools import (
    THREAD_HISTORY_SEARCH,
    history_hint,
    thread_history_search_definition,
)
from packages.agent_runtime.models import Agent
from packages.agent_runtime.publish import _resolved_spec, _validate_runtime_config
from packages.agent_runtime.runtime import _AgentRunGraph
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.snapshots import ResolvedKnowledgeSnapshot
from packages.model_gateway.contracts import ModelCapabilities
from packages.model_gateway.profile_resolution import ResolvedModelProfile
from packages.threads.context import _terms
from packages.tools.contracts import ToolApprovalPolicy, ToolEffect

_WORKSPACE = UUID("00000000-0000-0000-0000-000000000011")
_PROFILE = UUID("00000000-0000-0000-0000-000000000012")


def _agent(**overrides) -> Agent:
    values = {
        "id": UUID("00000000-0000-0000-0000-000000000010"),
        "workspace_id": _WORKSPACE,
        "name": "Support Agent",
        "system_prompt": "Answer from approved enterprise context.",
        "prompt_version": 1,
        "model_profile_id": _PROFILE,
        "knowledge_binding_mode": "PINNED",
        "model_retry_policy": {"max_attempts": 2},
        "retrieval_config": {},
        "runtime_config": {},
    }
    values.update(overrides)
    return Agent(**values)


def _spec(agent: Agent) -> dict:
    profile = ResolvedModelProfile(
        id=agent.model_profile_id,
        workspace_id=agent.workspace_id,
        provider_credential_id=UUID("00000000-0000-0000-0000-000000000001"),
        model="deepseek-chat",
        temperature=Decimal("0.1"),
        max_tokens=2_000,
        timeout_seconds=Decimal("30"),
        fallback_profile_id=None,
        capabilities=ModelCapabilities(
            tool_calling=True,
            streaming=True,
            structured_output=True,
            max_context_tokens=64_000,
        ),
    )
    snapshot = ResolvedKnowledgeSnapshot(
        snapshot_id=UUID("00000000-0000-0000-0000-000000000002"),
        workspace_id=UUID("00000000-0000-0000-0000-000000000003"),
        knowledge_base_id=UUID("00000000-0000-0000-0000-000000000004"),
        content_hash="a" * 64,
        snapshot_schema_version=1,
        item_count=2,
        created_at=datetime.now(UTC),
    )
    return _resolved_spec(
        agent=agent,
        model=((profile,), {profile.id: "deepseek"}),
        snapshots=(snapshot,),
        tools=(),
    )


# ---------------------------------------------------------------------------
# 1. Publishing
# ---------------------------------------------------------------------------


def test_memory_defaults_to_off_and_leaves_the_published_spec_untouched() -> None:
    without = _spec(_agent())
    explicitly_off = _spec(
        _agent(
            runtime_config=_validate_runtime_config(
                {"memory": {"thread_history_search": False, "long_term_memory": False}}
            )
        )
    )

    assert "memory" not in without["runtime"]
    assert canonical_json_hash(without) == canonical_json_hash(explicitly_off)


def test_turning_memory_on_is_a_behaviour_change_and_moves_the_hash() -> None:
    off = _spec(_agent())
    on = _spec(
        _agent(runtime_config=_validate_runtime_config({"memory": {"thread_history_search": True}}))
    )

    assert on["runtime"]["memory"]["thread_history_search"] is True
    assert on["runtime"]["memory"]["long_term_memory"] is False
    assert canonical_json_hash(on) != canonical_json_hash(off)


@pytest.mark.parametrize(
    "memory",
    [
        {"thread_history_search": "yes"},
        {"thread_history_search": 1},
        {"unknown_flag": True},
        "on",
        [],
    ],
)
def test_a_memory_block_that_is_not_two_booleans_is_rejected(memory: object) -> None:
    with pytest.raises(AgentHubError):
        _validate_runtime_config({"memory": memory})


# ---------------------------------------------------------------------------
# 2. The freeze boundary
# ---------------------------------------------------------------------------


def test_a_version_published_before_memory_existed_parses_as_all_off() -> None:
    spec = _spec(_agent())
    spec["runtime"].pop("memory", None)

    frozen = parse_frozen_agent_spec(spec, workspace_id=_WORKSPACE)

    assert frozen.runtime["memory"] == {
        "thread_history_search": False,
        "long_term_memory": False,
    }


def test_the_switch_survives_the_freeze() -> None:
    agent = _agent(
        runtime_config=_validate_runtime_config({"memory": {"thread_history_search": True}})
    )

    frozen = parse_frozen_agent_spec(_spec(agent), workspace_id=_WORKSPACE)

    assert frozen.runtime["memory"]["thread_history_search"] is True


def test_a_flag_this_build_does_not_understand_is_refused_not_ignored() -> None:
    spec = _spec(_agent())
    spec["runtime"]["memory"] = {"thread_history_search": True, "telepathy": True}

    with pytest.raises(AgentHubError) as raised:
        parse_frozen_agent_spec(spec, workspace_id=_WORKSPACE)

    assert raised.value.code == "AGENT_VERSION_MODEL_BINDING_INVALID"


# ---------------------------------------------------------------------------
# 3. Offering the tool
# ---------------------------------------------------------------------------


@dataclass
class _Spec:
    runtime: dict


@dataclass
class _Run:
    thread_id: object


class _Provider:
    async def search(self, **_kwargs):  # pragma: no cover - never called here
        return ()


@dataclass
class _Service:
    thread_context_provider: object


def _gate(*, enabled: bool, thread: bool, provider: object) -> bool:
    runtime = _AgentRunGraph.__new__(_AgentRunGraph)
    runtime.run = _Run(thread_id=uuid4() if thread else None)
    runtime.service = _Service(thread_context_provider=provider)
    spec = _Spec(runtime={"memory": {"thread_history_search": enabled}})
    return _AgentRunGraph._thread_history_search_enabled(runtime, spec)


def test_the_tool_needs_the_switch_a_thread_and_a_searcher() -> None:
    assert _gate(enabled=True, thread=True, provider=_Provider()) is True
    # Off by default is the whole point of the switch.
    assert _gate(enabled=False, thread=True, provider=_Provider()) is False
    # A single-shot Playground run has no thread to search, and must stay
    # byte-identical to what it was before memory existed.
    assert _gate(enabled=True, thread=False, provider=_Provider()) is False
    # Composition that never wired a searcher offers nothing rather than
    # offering a tool that fails on first use.
    assert _gate(enabled=True, thread=True, provider=None) is False
    assert _gate(enabled=True, thread=True, provider=object()) is False


def test_the_offered_tool_is_a_read_that_never_asks_for_approval() -> None:
    definition = thread_history_search_definition()

    assert definition.identity == THREAD_HISTORY_SEARCH
    assert definition.effect is ToolEffect.READ
    assert definition.approval_policy is ToolApprovalPolicy.NEVER
    assert definition.input_schema["additionalProperties"] is False
    # Derived, not random: two runs of the same build must agree on it.
    assert definition.revision_id == thread_history_search_definition().revision_id
    assert definition.spec_hash == thread_history_search_definition().spec_hash


def test_the_hint_names_the_tool_and_counts_what_was_dropped() -> None:
    dropped = history_hint(turns_dropped_by_window=7)
    none_dropped = history_hint(turns_dropped_by_window=0)

    assert "7" in dropped
    assert THREAD_HISTORY_SEARCH in dropped
    assert THREAD_HISTORY_SEARCH in none_dropped
    assert "7" not in none_dropped


# ---------------------------------------------------------------------------
# 4. Tokenization
# ---------------------------------------------------------------------------


def test_a_chinese_question_becomes_bigrams_not_one_lexeme() -> None:
    # This is the whole reason the search does not use to_tsquery: PostgreSQL
    # would reduce the phrase below to a single token, and a turn that said
    # "预算是 30 万" would not match a question asking 上次的预算.
    terms = _terms("上次的预算")

    assert terms == ["上次", "次的", "的预", "预算"]
    assert any(term in "我们上次说过预算是30万" for term in terms)


def test_terms_drop_punctuation_duplicates_and_single_characters() -> None:
    assert _terms("budget, BUDGET; the budget?") == ["budget", "the"]
    assert _terms("a b c") == []
    assert _terms("") == []
    assert len(_terms(" ".join(f"term{index}" for index in range(20)))) == 8
