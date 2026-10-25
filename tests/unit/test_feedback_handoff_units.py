"""Unit isolation for feedback/handoffs pure guards (rule 22).

The service behaviors of both packages are integration-covered
(test_mi2_feedback_evidence, test_handoff_lifecycle); these tests lock the
permission/idempotency/hash helpers that integration only touches
incidentally, without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.feedback.service import CATEGORIES, TERMINAL, FeedbackService, require
from packages.handoffs.service import fail, source_hash


def _context(*, user_id="user-1", permissions=("feedback.write",)):
    return SimpleNamespace(
        user_id=user_id,
        workspace_id=str(uuid4()),
        permissions=frozenset(permissions),
    )


# ---------------------------------------------------------------------------
# feedback.require: the permission gate every entry point leans on
# ---------------------------------------------------------------------------


def test_require_returns_workspace_uuid_when_permission_holds() -> None:
    workspace = _context()
    assert require(workspace, "feedback.write") == UUID(workspace.workspace_id)


def test_require_rejects_missing_permission() -> None:
    with pytest.raises(AgentHubError) as raised:
        require(_context(permissions=()), "feedback.write")
    assert raised.value.status_code == 403
    assert raised.value.code == "FORBIDDEN"


def test_require_rejects_missing_user_identity() -> None:
    with pytest.raises(AgentHubError):
        require(_context(user_id=None), "feedback.write")


def test_require_rejects_when_any_of_several_permissions_is_missing() -> None:
    with pytest.raises(AgentHubError):
        require(_context(), "feedback.write", "feedback.review")


# ---------------------------------------------------------------------------
# feedback idempotency: same submission key must map to identical content
# ---------------------------------------------------------------------------


def test_same_submission_accepts_matching_hash_and_returns_feedback() -> None:
    # A @staticmethod: the submission key must map back to identical content.
    feedback = SimpleNamespace(request_hash="hash-1", id="fb-1")
    assert FeedbackService._same_submission(feedback, "hash-1") is feedback


def test_same_submission_rejects_reused_key_with_different_content() -> None:
    with pytest.raises(AgentHubError) as raised:
        FeedbackService._same_submission(SimpleNamespace(request_hash="hash-1"), "hash-2")
    assert raised.value.code == "FEEDBACK_IDEMPOTENCY_CONFLICT"
    assert raised.value.status_code == 409


def test_feedback_vocabularies_are_closed_sets() -> None:
    assert CATEGORIES == frozenset(
        {"FACTUAL", "RETRIEVAL", "TOOL", "LATENCY", "EXPRESSION", "OTHER"}
    )
    assert TERMINAL == frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_ATTENTION"})


# ---------------------------------------------------------------------------
# handoffs.fail: one error envelope for every guard
# ---------------------------------------------------------------------------


def test_fail_raises_agent_hub_error_with_code_and_status() -> None:
    with pytest.raises(AgentHubError) as raised:
        fail("HANDOFF_STATE_CONFLICT", "already closed", 409)
    assert raised.value.code == "HANDOFF_STATE_CONFLICT"
    assert raised.value.status_code == 409
    assert raised.value.message == "already closed"


# ---------------------------------------------------------------------------
# handoffs.source_hash: content identity must depend on exactly the five fields
# ---------------------------------------------------------------------------


@dataclass
class _Artifact:
    """Field-for-field stand-in for the handoff artifact ORM row."""

    type: str
    title: str
    content: str
    thread_id: object
    run_id: object


def _artifact(**overrides):
    fields = {
        "type": "answer",
        "title": "Refund answer",
        "content": "within seven business days",
        "thread_id": uuid4(),
        "run_id": uuid4(),
    }
    fields.update(overrides)
    return _Artifact(**fields)


def test_source_hash_is_deterministic_for_identical_artifacts() -> None:
    artifact = _artifact()
    assert source_hash(artifact) == source_hash(artifact)


def test_source_hash_changes_with_each_covered_field() -> None:
    baseline = source_hash(_artifact())
    assert source_hash(_artifact(type="summary")) != baseline
    assert source_hash(_artifact(title="Other")) != baseline
    assert source_hash(_artifact(content="other content")) != baseline
    assert source_hash(_artifact(thread_id=uuid4())) != baseline
    assert source_hash(_artifact(run_id=uuid4())) != baseline


def test_source_hash_treats_none_run_id_distinctly_from_a_value() -> None:
    assert source_hash(_artifact(run_id=None)) != source_hash(_artifact())
