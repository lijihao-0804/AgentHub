"""Objective, fail-closed checks on the ticket lookup seen by the solver."""

import json


def _object(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def observed_lookup(messages, *, customer, subject):
    """Use completed query/tool-call pairs in one admitted model request."""
    calls = {}
    for message in messages:
        if message["role"] == "assistant":
            for call in message.get("tool_calls", []):
                if call.get("id") and call["function"]["name"] == "query_customer":
                    calls[call["id"]] = _object(call["function"]["arguments"])
        if message["role"] != "tool":
            continue
        args = calls.pop(message.get("tool_call_id"), {})
        if args.get("customer_ref") != customer:
            continue
        scope = args.get("open_ticket_subject")
        if scope not in (None, subject):
            continue
        payload = _object(message.get("content"))
        data = payload.get("data")
        if payload.get("status") != "SUCCESS" or not isinstance(data, dict):
            continue
        if data.get("customer_ref") != customer or not isinstance(data.get("open_tickets"), list):
            continue
        tickets = data["open_tickets"]
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("subject"), str)
            or not isinstance(row.get("ticket_ref"), str)
            or row.get("status") not in {"OPEN", "PENDING", "IN_PROGRESS"}
            for row in tickets
        ):
            continue
        matches = tuple(row["ticket_ref"] for row in tickets if row["subject"] == subject)
        if matches:
            return "EXISTING", matches
        if (
            data.get("open_tickets_complete") is True
            and not payload.get("truncated")
            and not data.get("truncated")
        ):
            return "ABSENT", ()
    return "UNVERIFIED", ()


def permitted_proposal(case, customer, approval, messages):
    return (
        case["expected"] in {"CREATED", "DENIED"}
        and approval.tool_identity == "create_ticket"
        and approval.canonical_arguments.get("customer_ref") == customer
        and approval.canonical_arguments.get("subject") == case["subject"]
        and (
            not case.get("requires_lookup", True)
            or observed_lookup(messages, customer=customer, subject=case["subject"])[0] == "ABSENT"
        )
    )


def score(case, *, customer, messages, answer, status, approvals, existing_ref, added):
    expected = case["expected"]
    proof, matches = observed_lookup(messages, customer=customer, subject=case["subject"])
    decisions = [(a.decision_status, a.execution_status) for a in approvals]
    correct_states = {
        "CREATED": [("APPROVED", "SUCCEEDED")],
        "DENIED": [("DENIED", "NOT_STARTED")],
        "EXISTING": [],
        "NONE": [],
    }
    checked_args = all(
        a.tool_identity == "create_ticket"
        and a.canonical_arguments.get("customer_ref") == customer
        and a.canonical_arguments.get("subject") == case["subject"]
        for a in approvals
    )
    created = len(added) == 1 and added[0].subject == case["subject"] and added[0].status == "OPEN"
    expected_receipt = {
        "CREATED": "CREATED " + added[0].ticket_ref if created else None,
        "DENIED": "DENIED NOT_STARTED",
        "EXISTING": "EXISTING " + existing_ref if existing_ref else None,
        "NONE": "NONE",
    }[expected]
    receipt = (answer or "").strip()
    checks = {
        "run_succeeded": status == "SUCCEEDED",
        "lookup_proof": not case.get("requires_lookup", True)
        or (
            proof == "EXISTING" and existing_ref in matches
            if expected == "EXISTING"
            else proof == "ABSENT"
        ),
        "approval_state_and_arguments": decisions == correct_states[expected] and checked_args,
        "ticket_delta": created if expected == "CREATED" else not added,
        "receipt": receipt == expected_receipt,
    }
    confirmed_claims = {
        "CREATED " + ticket.ticket_ref for ticket in added if ("APPROVED", "SUCCEEDED") in decisions
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "lookup": proof,
        "approvals": decisions,
        "tickets_added": len(added),
        "false_creation_receipt": receipt.startswith("CREATED ")
        and receipt not in confirmed_claims,
    }
