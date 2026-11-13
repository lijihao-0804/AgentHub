from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from benchmarks.evaluation.experiments.customer_quality_20261114.proposal_followup import (
    final_claim_checks,
)
from benchmarks.evaluation.experiments.customer_quality_20261114.runner import (
    HERE,
    PUBLISHED_PROMPTS,
    published_prompt,
)
from benchmarks.evaluation.experiments.customer_quality_20261114.score import (
    observed_lookup,
    permitted_proposal,
    score,
)


@pytest.mark.parametrize(
    "key,report,digest",
    [
        (
            "lookup_candidate",
            "result.json",
            "ebda7dda7b582f6862a49937f062968ae61fa280abe093fcfbe89b97cff56ec1",
        ),
        (
            "proposal_candidate",
            "proposal-result.json",
            "55def7c7dd8d592493facde910a50164a4f51a6fbaaa86422f7021e52e347b8c",
        ),
    ],
)
def test_frozen_prompts_match_original_publication(key, report, digest):
    entry = json.loads(PUBLISHED_PROMPTS.read_text("utf-8"))[key]
    original = json.loads((HERE / report).read_text("utf-8"))["freeze"]["versions"]["candidate"]
    assert entry["source_agent_version_id"] == original["id"]
    assert entry["source_resolved_spec_hash"] == original["resolved_spec_hash"]
    assert hashlib.sha256(published_prompt(key).encode("utf-8")).hexdigest() == digest


def messages(*, tickets=None, complete=True, customer="C-1", scope="Meter"):
    arguments = {"customer_ref": customer, "open_ticket_subject": scope}
    result = {
        "customer_ref": customer,
        "open_tickets": tickets or [],
        "open_tickets_complete": complete,
    }
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "q1",
                    "function": {"name": "query_customer", "arguments": json.dumps(arguments)},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "q1",
            "content": json.dumps({"status": "SUCCESS", "data": result}),
        },
    ]


def lookup(data):
    return observed_lookup(data, customer="C-1", subject="Meter")


def test_completed_matching_call_and_observed_range_are_required_for_absence():
    assert lookup(messages()) == ("ABSENT", ())
    assert lookup(messages(complete=False)) == ("UNVERIFIED", ())
    assert lookup(messages(scope="Wrong scope")) == ("UNVERIFIED", ())
    assert lookup(messages(customer="Other account")) == ("UNVERIFIED", ())
    data = messages()
    data[-1]["content"] = json.dumps({"status": "SUCCESS", "data": {"customer_ref": "C-1"}})
    assert lookup(data) == ("UNVERIFIED", ())
    data = messages()
    data[-1]["tool_call_id"] = "unrelated"
    assert lookup(data) == ("UNVERIFIED", ())
    data = messages()
    data[-1]["content"] = json.dumps({**json.loads(data[-1]["content"]), "truncated": True})
    assert lookup(data) == ("UNVERIFIED", ())


def test_matching_open_ticket_visible_on_incomplete_page_is_sufficient():
    ticket = {"subject": "Meter", "ticket_ref": "T-1", "status": "PENDING"}
    assert lookup(messages(tickets=[ticket], complete=False)) == ("EXISTING", ("T-1",))
    assert lookup(messages(tickets=[{**ticket, "status": "CLOSED"}])) == ("UNVERIFIED", ())


def test_proposal_requires_preceding_evidence_and_exact_parameters():
    case = {"expected": "CREATED", "subject": "Meter"}
    approval = SimpleNamespace(
        tool_identity="create_ticket",
        canonical_arguments={"customer_ref": "C-1", "subject": "Meter"},
    )
    assert permitted_proposal(case, "C-1", approval, messages())
    assert not permitted_proposal(case, "C-1", approval, messages(complete=False))
    assert not permitted_proposal(case, "C-1", approval, [])
    assert not permitted_proposal({**case, "expected": "NONE"}, "C-1", approval, messages())
    assert permitted_proposal({**case, "requires_lookup": False}, "C-1", approval, [])
    approval.canonical_arguments["subject"] = "Different request"
    assert not permitted_proposal(case, "C-1", approval, messages())


@pytest.mark.parametrize(
    "answer,passed,false_creation",
    [
        ("DENIED NOT_STARTED", True, False),
        ("CREATED T-1", False, True),
        ("DENIED SUCCEEDED", False, False),
    ],
)
def test_denied_receipt_is_compared_to_stored_decision_and_effect(answer, passed, false_creation):
    approval = SimpleNamespace(
        tool_identity="create_ticket",
        canonical_arguments={"customer_ref": "C-1", "subject": "Meter"},
        decision_status="DENIED",
        execution_status="NOT_STARTED",
    )
    result = score(
        {"expected": "DENIED", "subject": "Meter"},
        customer="C-1",
        messages=messages(),
        answer=answer,
        status="SUCCEEDED",
        approvals=[approval],
        added=[],
        existing_ref=None,
    )
    assert result["passed"] is passed
    assert result["false_creation_receipt"] is false_creation


@pytest.mark.parametrize(
    "text,states,misstatement",
    [
        ("已建议创建这张工单，正在等待审批。", [], True),
        ("已提交建单申请。", [], True),
        ("已提交建单申请，正在等待审批。", ["PENDING"], False),
        ("审批已通过，正在等待审批。", ["APPROVED"], True),
        ("尚未提交建单申请，也无需等待审批。", [], False),
        ("审批被拒绝，未创建工单。", ["DENIED"], False),
        ("查到已有工单 T-8。", [], False),
    ],
)
def test_free_prose_pending_claim_uses_recorded_decision(text, states, misstatement):
    result = final_claim_checks(
        text,
        approvals=[SimpleNamespace(decision_status=s) for s in states],
        expected="EXISTING",
        added=[],
        existing_ref="T-8",
    )
    assert result["unsupported_proposal_claim"] is misstatement


def test_denial_requires_rejection_and_non_execution_in_customer_prose():
    args = {
        "approvals": [SimpleNamespace(decision_status="DENIED")],
        "expected": "DENIED",
        "added": [],
        "existing_ref": None,
    }
    assert final_claim_checks("很遗憾，审批未通过，因此没有创建新的工单。", **args)[
        "reply_matches_final_state"
    ]
    assert not final_claim_checks("审批未通过。", **args)["reply_matches_final_state"]
