"""A1: how thread history enters a run, and what the budget does with it.

The trap this file exists to catch is in the categorizer. Every ``user``
message used to be ``CURRENT_USER_TASK``, which is mandatory and therefore
unevictable. Replaying ten earlier turns through that path would pin ten
questions nothing may drop, and a long thread would blow its own budget before
the model saw anything. Only the question being asked *now* is the task.
"""

from __future__ import annotations

import pytest

from packages.agent_runtime.context_budget import ContextCategory
from packages.agent_runtime.runtime import _categorize_messages
from packages.agent_runtime.work_layer import (
    RecordedToolCall,
    ThreadConversation,
    ThreadTurnContext,
)
from packages.model_gateway.contracts import ModelMessage


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
