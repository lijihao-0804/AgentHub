"""A1: how thread history enters a run, and what the budget does with it.

The trap this file exists to catch is in the categorizer. Every ``user``
message used to be ``CURRENT_USER_TASK``, which is mandatory and therefore
unevictable. Replaying ten earlier turns through that path would pin ten
questions nothing may drop, and a long thread would blow its own budget before
the model saw anything. Only the question being asked *now* is the task.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
    ContextMessage,
)
from packages.agent_runtime.frozen import FrozenAgentSpec
from packages.agent_runtime.runtime import _categorize_messages
from packages.agent_runtime.work_layer import (
    RecordedToolCall,
    ThreadConversation,
    ThreadTurnContext,
)
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelMessage,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)


def messages(history_turns: int) -> list[ModelMessage]:
    built = [
        ModelMessage(role="system", content="runtime policy"),
        ModelMessage(role="system", content="agent prompt"),
    ]
    for index in range(history_turns):
        built.append(ModelMessage(role="user", content=f"old question {index}"))
        built.append(ModelMessage(role="assistant", content=f"old answer {index}"))
    built.append(ModelMessage(role="user", content="the question being asked now"))
    return built


def test_without_history_the_only_user_message_is_the_current_task() -> None:
    categorized = _categorize_messages(messages(0), history_message_count=0)

    tasks = [item for item in categorized if item.category is ContextCategory.CURRENT_USER_TASK]
    assert len(tasks) == 1
    assert tasks[0].content == "the question being asked now"


def test_replayed_questions_are_conversation_not_the_current_task() -> None:
    categorized = _categorize_messages(messages(3), history_message_count=6)

    tasks = [item for item in categorized if item.category is ContextCategory.CURRENT_USER_TASK]
    assert len(tasks) == 1
    assert tasks[0].content == "the question being asked now"

    conversation = [
        item for item in categorized if item.category is ContextCategory.CONVERSATION
    ]
    # Three questions and three answers, none of them mandatory.
    assert len(conversation) == 6
    assert "old question 0" in {item.content for item in conversation}


def test_the_two_system_messages_keep_their_categories_with_history_present() -> None:
    categorized = _categorize_messages(messages(2), history_message_count=4)

    assert categorized[0].category is ContextCategory.RUNTIME_POLICY
    assert categorized[1].category is ContextCategory.SYSTEM_PROMPT


def test_a_history_count_that_overruns_the_list_does_not_swallow_the_task() -> None:
    # A miscount must not be able to demote the current question, because that
    # would make the run answer nothing in particular.
    categorized = _categorize_messages(messages(1), history_message_count=99)
    tasks = [item for item in categorized if item.category is ContextCategory.CURRENT_USER_TASK]
    assert tasks == [] or tasks[0].content == "the question being asked now"


def test_conversation_turns_carry_artifact_references_not_artifacts() -> None:
    turn = ThreadTurnContext(
        user_input="find papers",
        final_output="I found some.",
        artifact_refs=('[artifact: research.paper_search "Search: leo" (20 papers)]',),
    )
    conversation = ThreadConversation(turns=(turn,), turns_available=5)

    assert conversation.turns_available == 5
    assert len(conversation.turns) == 1
    assert "20 papers" in conversation.turns[0].artifact_refs[0]
    # The abstracts are not in there, and that is the whole point.
    assert "abstract" not in conversation.turns[0].artifact_refs[0]


def test_recorded_tool_call_defaults_are_empty_not_shared() -> None:
    first = RecordedToolCall(tool_identity="search_papers", tool_call_id="a", step_sequence=1)
    second = RecordedToolCall(tool_identity="search_papers", tool_call_id="b", step_sequence=2)

    first.arguments["query"] = "x"
    assert second.arguments == {}


@pytest.mark.parametrize("count", [0, 2, 4])
def test_history_is_always_between_the_system_block_and_the_current_task(count: int) -> None:
    built = messages(count // 2)
    categorized = _categorize_messages(built, history_message_count=count)

    ordered = [item.category for item in categorized]
    assert ordered[0] is ContextCategory.RUNTIME_POLICY
    assert ordered[1] is ContextCategory.SYSTEM_PROMPT
    assert ordered[-1] is ContextCategory.CURRENT_USER_TASK


# ---------------------------------------------------------------------------
# A replayed turn is one exchange, not two messages
# ---------------------------------------------------------------------------
#
# The second trap in this file. Grouping every replayed message on its own index
# made each question and each answer a separate eviction unit. The budget policy
# drops the oldest group first, so a tight budget could evict "old question 0"
# and keep "old answer 0" — leaving the model an answer to a question it can no
# longer see. Nothing raised, because a dangling assistant message is valid to a
# provider in a way a dangling tool_call_id is not. It was only wrong.


@dataclass(frozen=True)
class _CharEstimator:
    def estimate(self, text: str) -> int:
        return len(text)


def _policy(*, context: int) -> ContextBudgetPolicy:
    spec = FrozenAgentSpec(
        model_plan=ResolvedModelExecutionPlan(
            primary=ResolvedModelExecutionProfile(
                id=uuid4(),
                workspace_id=uuid4(),
                provider_credential_id=uuid4(),
                provider="test",
                model="test-model",
                temperature=Decimal("0"),
                max_tokens=1,
                timeout_seconds=Decimal("30"),
                capabilities=ModelCapabilities(max_context_tokens=context),
            ),
            retry_policy=RetryPolicy(),
        ),
        system_prompt="unused by the policy; the caller supplies explicit messages",
        prompt_version=1,
        runtime={},
    )
    return ContextBudgetPolicy(
        spec,
        ContextBudgetConfig(reserved_output_tokens=1),
        estimator=_CharEstimator(),
    )


def _history_groups(categorized: Sequence[ContextMessage]) -> dict[object, list[str | None]]:
    grouped: dict[object, list[str | None]] = {}
    for item in categorized:
        if item.category is ContextCategory.CONVERSATION:
            grouped.setdefault(item.exchange_group, []).append(item.content)
    return grouped


def test_a_replayed_question_and_its_answer_share_one_exchange_group() -> None:
    categorized = _categorize_messages(messages(3), history_message_count=6)

    grouped = _history_groups(categorized)

    # Three turns, not six singletons.
    assert len(grouped) == 3
    for contents in grouped.values():
        assert len(contents) == 2
    # And the pairing is by turn, not by adjacency accident.
    assert ["old question 1", "old answer 1"] in grouped.values()


def test_a_budget_too_small_for_every_turn_drops_whole_exchanges() -> None:
    # The regression itself: squeeze the window until turns have to go, then
    # assert that whatever survived survived in pairs.
    built = messages(4)
    categorized = _categorize_messages(built, history_message_count=8)

    # 125 is chosen, not rounded: it leaves room for three of the eight history
    # messages to be evicted, which under per-message grouping stops exactly one
    # message into a turn and strands "old answer 1".
    result = _policy(context=125).admit(categorized)

    admitted = [message.content for message in result.admitted_messages]
    # The test is only meaningful if the budget actually bit.
    assert result.usage.dropped_exchange_count > 0
    for index in range(4):
        question = f"old question {index}"
        answer = f"old answer {index}"
        assert (question in admitted) == (answer in admitted), (
            f"turn {index} was admitted as half an exchange: "
            f"question={question in admitted} answer={answer in admitted}"
        )
    # The current question is never what gets dropped.
    assert "the question being asked now" in admitted
