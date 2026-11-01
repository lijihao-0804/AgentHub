"""Research-only QMSum preparation and failure gates; no provider calls."""

import json
from dataclasses import replace

import pytest

from benchmarks.evaluation.memory_qmsum_protocol import (
    QMSumProtocolError,
    plan_offline,
)
from benchmarks.evaluation.memory_qmsum_summary import prepare_summary, validate_research_summary
from benchmarks.evaluation.memory_trial_policy import create_trial_policy
from packages.agent_runtime.context_budget import ApproxTokenEstimator
from tests.unit.test_memory_qmsum_protocol import _source


def _prepared_case():
    source = _source()  # Synthetic fixture; never used as evaluation data.
    question = source.questions[0]
    first, second = question.transcript
    second = replace(second, content=second.content + " x" * 41_000)
    question = replace(question, transcript=(first, second))
    source = replace(source, questions=(question, *source.questions[1:]))
    policy = create_trial_policy()
    plan = plan_offline(source, admission=policy, token_counter=ApproxTokenEstimator())
    return source, plan, question, policy


@pytest.fixture
def prepared_case():
    return _prepared_case()


def _prepare(source, plan, question, policy):
    return prepare_summary(
        source=source,
        plan=plan,
        question_id=question.question_id,
        protected_excerpts=("[0] Chair: We must keep the 20 minute limit.",),
        policy=policy,
    )


def test_summary_request_retains_source_attribution_and_reviewed_exact_text(prepared_case):
    source, plan, question, policy = prepared_case
    prepared = _prepare(source, plan, question, policy)
    assert prepared.admitted.messages == prepared.messages
    assert not prepared.admitted.usage.truncated
    assert prepared.context.sources[0].role == "MEETING_TRANSCRIPT"
    assert prepared.context.sources[0].occurred_at == "UNKNOWN"
    assert prepared.context.protected_facts[0].text in prepared.context.sources[0].content
    assert prepared.context.completed_actions == ()
    assert "Synthetic reference answer" not in prepared.messages[1].content


def test_only_source_backed_protected_excerpts_are_accepted(prepared_case):
    source, plan, question, policy = prepared_case
    with pytest.raises(QMSumProtocolError, match="absent from the frozen transcript"):
        prepare_summary(
            source=source,
            plan=plan,
            question_id=question.question_id,
            protected_excerpts=("invented quote",),
            policy=policy,
        )
    with pytest.raises(QMSumProtocolError, match="protected turn"):
        prepare_summary(
            source=source,
            plan=plan,
            question_id=question.question_id,
            protected_excerpts=("We must keep the 20 minute limit.",),
            policy=policy,
        )
    with pytest.raises(QMSumProtocolError, match="bounded frozen tuple"):
        prepare_summary(
            source=source,
            plan=plan,
            question_id=question.question_id,
            protected_excerpts=(),
            policy=policy,
        )


def test_a_changed_question_or_transcript_cannot_reuse_the_frozen_baseline(prepared_case):
    source, plan, question, policy = prepared_case
    changed = replace(question, question=question.question + " changed")
    source = replace(source, questions=(changed, *source.questions[1:]))
    with pytest.raises(QMSumProtocolError, match="baseline does not match"):
        _prepare(source, plan, changed, policy)


def test_candidate_requires_complete_valid_response_and_preserves_protected_fact(prepared_case):
    source, plan, question, policy = prepared_case
    prepared = _prepare(source, plan, question, policy)
    source_id = prepared.context.sources[0].source_id
    response = json.dumps(
        {
            "status": "OK",
            "goal": {"text": "continue the task", "source_ids": [source_id]},
            "decisions": [],
            "supported_facts": [],
            "conflicts": [],
            "open_questions": [],
            "completed_actions_refs": [],
            "next_steps": [],
            "omitted_source_ids": [],
        }
    )
    assert validate_research_summary(prepared, content=response, finish_reason="length") == (
        None,
        None,
    )
    validation, candidate = validate_research_summary(
        prepared, content=response, finish_reason="stop"
    )
    assert validation is not None and validation.valid
    assert candidate is not None
    assert "We must keep the 20 minute limit." in validation.candidate.final_view_text

    bad_response = response.replace(source_id, "unknown-source")
    validation, candidate = validate_research_summary(
        prepared, content=bad_response, finish_reason="stop"
    )
    assert validation is not None and not validation.valid
    assert candidate is None
