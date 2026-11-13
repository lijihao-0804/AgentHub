"""Exercise the support desk with synthetic records and the configured Beijing Aliyun model.

Run against a dedicated AgentHub API with the database and checkpoint migrations applied:
    python -m scripts.support_api_smoke --api-url http://127.0.0.1:8000/api/v1

Creates a fresh workspace, its two users and one customer through public HTTP. Submits three
distinct user turns, with at most four model rounds per turn, one attempt and no fallback.
The new provider credential is disabled and its secret replaced at the end. Output contains
identifiers, recorded outcomes and token usage. An incomplete or incorrect case exits nonzero.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
from uuid import uuid4

import httpx

from packages.model_gateway.configured_route import ConfiguredRouteError
from scripts.aliyun_api_smoke import (
    MODEL,
    SmokeFailure,
    aliyun_route,
    check_url,
    create_model_profile,
    create_provider_credential,
    identifier,
    request,
    require,
)

SUBJECT = "寄件柜重复扣费复核"
TOOLS = ("query_customer", "create_ticket")


def register(client):
    credentials = {
        "email": f"desk-{uuid4().hex}@example.test",
        "password": secrets.token_urlsafe(32),
    }
    account = request(client, "POST", "auth/register", credentials)
    client.headers["Authorization"] = "Bearer " + account["access_token"]
    return identifier(account["user_id"]), credentials


def require_conflict(response, code):
    require(
        response.status_code == 409 and response.json()["error"]["code"] == code,
        f"expected HTTP 409 {code}, got {response.status_code}",
    )


def publish(client, workspace, profile_id):
    template = request(client, "GET", workspace + "/agent-templates/support")
    require(set(TOOLS).issubset(template["tool_hints"]), "template lacks customer or ticket tool")
    bindings = []
    for identity in TOOLS:
        tool = request(client, "POST", workspace + "/tools", {"builtin_identity": identity})
        bindings.append(
            {
                "tool_id": identifier(tool["id"]),
                "tool_revision_id": identifier(tool["current_revision_id"]),
            }
        )
    agent = request(
        client,
        "POST",
        workspace + "/agents",
        {
            "name": template["name"],
            "model_profile_id": profile_id,
            "system_prompt": template["system_prompt"],
            "model_retry_policy": {"max_attempts": 1},
            "runtime_config": {"max_steps": 4, "max_tool_calls": 4, "max_parallel_reads": 1},
        },
    )
    agent_path = workspace + "/agents/" + identifier(agent["id"])
    request(client, "PUT", agent_path + "/tool-bindings", bindings)
    publication = request(client, "POST", agent_path + "/publish")
    version = request(
        client, "GET", agent_path + "/versions/" + identifier(publication["agent_version_id"])
    )
    spec = version["resolved_spec"]
    require(
        version["resolved_spec_hash"] == publication["resolved_spec_hash"]
        and re.fullmatch("[a-f0-9]{64}", version["resolved_spec_hash"])
        and spec["prompt"]["system_prompt"] == template["system_prompt"]
        and {tool["tool_revision_id"] for tool in spec["tools"]}
        == {b["tool_revision_id"] for b in bindings},
        "published template or tool revisions differ from selected settings",
    )
    return agent_path, version


def register_customer(operator, workspace):
    endpoint = workspace + "/support/customers"
    require(request(operator, "GET", endpoint)["total"] == 0, "new workspace already has customers")
    data = {"customer_ref": "DESK-072", "name": "陈岚", "email": "recipient@example.test"}
    customer = request(operator, "POST", endpoint, data)
    replay = operator.post(endpoint, json=data)
    require(
        replay.status_code == 200 and replay.json() == customer,
        "customer registration replay mismatch",
    )
    require_conflict(
        operator.post(endpoint, json={**data, "name": "另一位联系人"}), "CUSTOMER_REF_CONFLICT"
    )
    page = request(operator, "GET", endpoint + "?q=DESK-072&limit=1&offset=0")
    require(page == {"items": [customer], "total": 1}, "customer directory readback mismatch")
    return customer


def submit(operator, agent_path, workspace, version, report, case, prompt):
    thread = request(
        operator, "POST", agent_path + "/threads", {"title": SUBJECT, "kind": "support"}
    )
    turns = workspace + "/threads/" + identifier(thread["id"]) + "/turns"
    payload = {"input_text": prompt, "client_token": uuid4().hex}
    submitted = request(operator, "POST", turns, payload)
    run_id = identifier(submitted["run_id"])
    record = {"case": case, "thread_id": thread["id"], "run_id": run_id}
    report["runs"].append(record)
    run_path = workspace + "/agent-runs/" + run_id
    run = request(operator, "GET", run_path)
    record.update(
        status=run["status"], model_steps=run["model_step_count"], tokens=run["total_tokens"]
    )
    require(
        not submitted["reused"]
        and run["resolved_spec_hash"] == version["resolved_spec_hash"]
        and run["agent_version_id"] == version["id"],
        case + " was not a new run of the published version",
    )
    replay = request(operator, "POST", turns, payload)
    require(
        replay["reused"]
        and replay["run_id"] == run_id
        and replay["turn"]["id"] == submitted["turn"]["id"],
        case + " did not reuse the original turn",
    )
    steps = request(operator, "GET", run_path + "/steps")
    looked_up = [
        step["sequence_number"]
        for step in steps
        if any(
            result.get("tool_identity") == "query_customer" and result.get("status") == "SUCCESS"
            for result in step["safe_metadata"].get("tool_results", [])
        )
    ]
    require(bool(looked_up), case + " has no successful customer lookup")
    if case != "existing":
        waits = [step["sequence_number"] for step in steps if step["kind"] == "APPROVAL_WAIT"]
        require(
            run["status"] == "WAITING_APPROVAL" and waits and min(looked_up) < min(waits),
            case + " did not look up the customer before approval",
        )
    return turns, run_path, record


def finish(operator, turns, run_path, record):
    run = request(operator, "GET", run_path)
    steps = request(operator, "GET", run_path + "/steps")
    model_steps = [s for s in steps if s["kind"] == "MODEL"]
    record.update(
        status=run["status"],
        model_steps=len(model_steps),
        input_tokens=run["total_input_tokens"],
        output_tokens=run["total_output_tokens"],
        tokens=run["total_tokens"],
    )
    require(
        run["status"] == "SUCCEEDED" and 0 < len(model_steps) == run["model_step_count"] <= 4,
        record["case"] + " did not reach a successful terminal run within the budget",
    )
    for step in model_steps:
        meta = step["safe_metadata"]
        require(
            step["status"] == "SUCCEEDED"
            and meta.get("model") == MODEL
            and meta.get("provider") == "openai-compatible"
            and meta.get("usage_source") == "generation"
            and isinstance(meta.get("total_tokens"), int)
            and meta["total_tokens"] > 0,
            record["case"] + " is missing a successful Aliyun model step with usage",
        )
    require(
        run["total_tokens"] == sum(s["safe_metadata"]["total_tokens"] for s in model_steps),
        "run and per-step usage totals differ",
    )
    (turn,) = request(operator, "GET", turns)
    require(
        turn["agent_run_id"] == run["id"] and turn["final_output"] == run["final_output"],
        "persisted turn and run replies differ",
    )
    return turn


def decide(
    operator, approver, workspace, run_path, identifiers, customer, tickets, before, decision
):
    (pending,) = request(approver, "GET", run_path + "/approvals")
    require(
        pending["requested_by"] == identifiers[0]
        and pending["decision_status"] == "PENDING"
        and pending["execution_status"] == "NOT_STARTED"
        and pending["tool_identity"] == "create_ticket"
        and pending["canonical_arguments"].get("customer_ref") == customer["customer_ref"]
        and pending["canonical_arguments"].get("subject") == SUBJECT
        and request(operator, "GET", tickets)["total"] == before,
        "pending approval does not match the customer, request or ticket count",
    )
    path = workspace + "/approvals/" + identifier(pending["id"]) + "/" + decision
    body = {"reason": "本次测试的补开请求未获批准。"} if decision == "deny" else None
    result = request(approver, "POST", path, body)
    expected = (
        ("APPROVED", "SUCCEEDED", 1) if decision == "approve" else ("DENIED", "NOT_STARTED", 0)
    )
    approval = result["approval"]
    require(
        result["run_id"] == pending["run_id"]
        and result["run_status"] == "SUCCEEDED"
        and approval["decided_by"] == identifiers[1] != approval["requested_by"]
        and (
            approval["decision_status"],
            approval["execution_status"],
            approval["execution_attempt_count"],
        )
        == expected,
        "decision or execution facts differ from the expected result",
    )
    replay = request(approver, "POST", path, body)
    require(
        replay["approval"] == approval and replay["run_id"] == result["run_id"],
        "replaying the completed decision changed the action",
    )
    return approval


def exercise(operator, owner, workspace, ids, agent_path, version, customer, report):
    tickets = workspace + "/support/customers/" + identifier(customer["id"]) + "/tickets"
    ref = customer["customer_ref"]
    condition = (
        f"客户编号 {ref}。请查是否已有准确主题为“{SUBJECT}”的未结工单。"
        "如果有，请告诉我原编号，不要重复开单。如果没有，请直接按原主题发起建单审批。"
        "批准并实际建成后请告知真正的工单号。若审批被拒，请告知“未创建”。"
    )
    require(
        request(operator, "GET", tickets) == {"items": [], "total": 0},
        "customer has pre-existing tickets",
    )
    turns, path, record = submit(
        operator, agent_path, workspace, version, report, "approved", condition
    )
    approval = decide(operator, owner, workspace, path, ids, customer, tickets, 0, "approve")
    turn = finish(operator, turns, path, record)
    result = approval["safe_result"]
    ticket_id = identifier(result["ticket_id"])
    ticket_ref = result["ticket_ref"]
    ticket_path = workspace + "/support/tickets/" + ticket_id
    (receipt,) = turn["ticket_requests"]
    require(
        receipt["approval_id"] == approval["id"]
        and receipt["ticket_id"] == ticket_id
        and receipt["ticket_ref"] == ticket_ref
        and receipt["decision_status"] == "APPROVED"
        and receipt["execution_status"] == "SUCCEEDED"
        and ticket_ref in turn["final_output"],
        "persisted approval, ticket receipt and agent reply disagree",
    )
    detail = request(operator, "GET", ticket_path)
    require(
        detail["id"] == ticket_id
        and detail["ticket_ref"] == ticket_ref
        and detail["subject"] == SUBJECT
        and detail["customer_id"] == customer["id"]
        and detail["status"] == "OPEN"
        and detail["version"] == 1,
        "new ticket content or version differs from the receipt",
    )
    report["ticket_id"], report["ticket_ref"] = ticket_id, ticket_ref
    record.update(decision="APPROVED", execution="SUCCEEDED")

    turns, path, record = submit(
        operator, agent_path, workspace, version, report, "existing", condition
    )
    turn = finish(operator, turns, path, record)
    require(
        ticket_ref in turn["final_output"]
        and turn["ticket_requests"] == []
        and request(operator, "GET", path + "/approvals") == []
        and request(operator, "GET", tickets)["total"] == 1,
        "existing open ticket was not returned without a duplicate",
    )
    for old_version, status in ((1, "IN_PROGRESS"), (2, "CLOSED")):
        patch = {"status": status, "expected_version": old_version}
        changed = request(operator, "PATCH", ticket_path, patch)
        require(
            changed["status"] == status
            and changed["version"] == old_version + 1
            and request(operator, "PATCH", ticket_path, patch) == changed,
            "status transition or replay did not preserve the expected version",
        )
    require_conflict(
        operator.patch(ticket_path, json={"status": "PENDING", "expected_version": 1}),
        "TICKET_VERSION_CONFLICT",
    )
    turns, path, record = submit(
        operator, agent_path, workspace, version, report, "denied", condition
    )
    denied = decide(operator, owner, workspace, path, ids, customer, tickets, 1, "deny")
    turn = finish(operator, turns, path, record)
    (receipt,) = turn["ticket_requests"]
    require(
        denied["safe_result"] is None
        and receipt["approval_id"] == denied["id"]
        and receipt["decision_status"] == "DENIED"
        and receipt["execution_status"] == "NOT_STARTED"
        and not receipt.get("ticket_id")
        and not receipt.get("ticket_ref")
        and "未创建" in turn["final_output"],
        "denied action has an inconsistent receipt or reply",
    )
    record.update(decision="DENIED", execution="NOT_STARTED")
    return tickets, ticket_path


def run(api_url, report):
    check_url(api_url)
    route = aliyun_route()
    options = {
        "base_url": api_url.rstrip("/") + "/",
        "timeout": 4 * float(route.timeout_seconds) + 30,
        "trust_env": False,
        "follow_redirects": False,
    }
    with httpx.Client(**options) as owner, httpx.Client(**options) as operator:
        owner_id, _ = register(owner)
        operator_id, operator_login = register(operator)
        org = request(owner, "POST", "organizations", {"name": "客服流程验收"})
        ws = request(
            owner,
            "POST",
            "workspaces",
            {"organization_id": identifier(org["id"]), "name": "寄件服务"},
        )
        workspace = "workspaces/" + identifier(ws["id"])
        report["workspace_id"] = ws["id"]
        request(
            owner,
            "POST",
            "organizations/" + org["id"] + "/members",
            {"user_id": operator_id, "role": "MEMBER"},
        )
        request(
            owner, "POST", workspace + "/members", {"user_id": operator_id, "role": "DEVELOPER"}
        )
        customer = register_customer(operator, workspace)
        credential_id = create_provider_credential(owner, workspace, route)
        try:
            profile_id = create_model_profile(owner, workspace, route, credential_id)
            agent_path, version = publish(owner, workspace, profile_id)
            report.update(version_id=version["id"], spec_sha256=version["resolved_spec_hash"])
            tickets, detail = exercise(
                operator,
                owner,
                workspace,
                (operator_id, owner_id),
                agent_path,
                version,
                customer,
                report,
            )
            with httpx.Client(**options) as reader:
                login = request(reader, "POST", "auth/login", operator_login)
                reader.headers["Authorization"] = "Bearer " + login["access_token"]
                row = request(reader, "GET", detail)
                page = request(reader, "GET", tickets)
                require(
                    row["id"] == report["ticket_id"]
                    and row["status"] == "CLOSED"
                    and row["version"] == 3
                    and page["total"] == 1
                    and len(page["items"]) == 1
                    and page["items"][0]["id"] == row["id"]
                    and page["items"][0]["status"] == "CLOSED",
                    "fresh login could not read the single closed ticket",
                )
                report.update(
                    fresh_login_readback=True,
                    final_status=row["status"],
                    final_version=row["version"],
                    customer_ticket_count=page["total"],
                )
        finally:
            endpoint = workspace + "/provider-credentials/" + credential_id
            request(owner, "PATCH", endpoint, {"enabled": False})
            request(
                owner, "POST", endpoint + "/rotate-secret", {"secret": secrets.token_urlsafe(32)}
            )
    report["passed"] = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="Dedicated AgentHub API ending in /api/v1")
    args = parser.parse_args()
    report = {"passed": False, "model": MODEL, "runs": []}
    try:
        run(args.api_url, report)
    except (SmokeFailure, ConfiguredRouteError) as exc:
        report["error"] = str(exc)
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
        report["error"] = type(exc).__name__
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
