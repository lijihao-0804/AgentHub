"""Reconcile archived support Runs, admitted requests and settled receipts."""

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

from .product_wire_replay import wire_bounds
from .runtime_trial import save

PUBLISHED = Path("benchmarks/public_rag/results/published")
HISTORICAL = PUBLISHED / "rag-product-evidence-20261114" / "paid"
OUTPUT = PUBLISHED / "rag-answer-contracts-v1"
OLD_SCRIPTED = Path(__file__).parent / "product-results" / "contract-final.zip"


def sha(body):
    return hashlib.sha256(body).hexdigest()


def visible(messages):
    delivered = []
    for msg in messages:
        if msg["role"] != "tool" or msg["name"] != "search_knowledge":
            continue
        data = json.loads(msg["content"]).get("data", {})
        for record in data.get("results", []):
            delivered.append(
                {
                    "chunk_id": record["chunk_id"],
                    "revision": record["document_revision_id"],
                    "snippet_sha256": sha(record["snippet"].encode()),
                }
            )
    return delivered


def inspect_history():
    manifest = json.loads((HISTORICAL / "manifest.json").read_bytes())
    for filename in (
        "refund-first-runs.zip",
        "refund-interruption.zip",
        "refund-scores.json",
        "billing.json",
    ):
        assert sha((HISTORICAL / filename).read_bytes()) == manifest["artifact_sha256"][filename]
    billing = json.loads((HISTORICAL / "billing.json").read_bytes())
    receipts = {w["id"]: w for w in billing["wires"] if w["stage"].startswith("refund_")}
    scores = json.loads((HISTORICAL / "refund-scores.json").read_bytes())["rows"]
    with ZipFile(HISTORICAL / "refund-interruption.zip") as interruption:
        recovered = json.loads(interruption.read("S0-038-readback.json"))
    recovered_steps = [json.loads(step["safe_metadata"]) for step in recovered["steps"]]
    checked = []
    with ZipFile(HISTORICAL / "refund-first-runs.zip") as runs:
        for score in scores:
            case = score["case_id"]
            raw = runs.read(case + ".json")
            r = json.loads(raw)
            assert r["run_id"] == score["run_id"] == r["run"]["id"] == r["turn"]["agent_run_id"]
            final = r["run"]["final_output"]
            assert final == r["turn"]["final_output"]
            calls = r["calls"]
            wires = r["physical_attempts"]
            assert len(calls) == len(wires)
            assert all(
                w == receipts[w["id"]] and w["task"] == case and w["status"] == "returned"
                for w in wires
            )
            if final:
                assert final == calls[-1]["response"]["content"] == score["answer"]
            proposals = []
            observations = {}
            capabilities = {}
            request_evidence = []
            for call in calls:
                req = call["request"]
                for t in req["tools"]:
                    capabilities[t["name"]] = t["parameters"].get("required", [])
                response = call.get("response") or {}
                proposals.extend(response.get("tool_calls", []))
                request_evidence.append(visible(req["messages"]))
                for msg in req["messages"]:
                    if msg["role"] == "tool" and msg["name"] != "search_knowledge":
                        observations[msg["tool_call_id"]] = {
                            "name": msg["name"],
                            "payload": json.loads(msg["content"]),
                        }
            approval = (r.get("decision") or {}).get("approval")
            usage = [r["run"].get("total_input_tokens"), r["run"].get("total_output_tokens")]
            if r["run_id"] == recovered["run"][0]["id"]:
                usage = [
                    sum(step.get(k, 0) for step in recovered_steps)
                    for k in ("input_tokens", "output_tokens")
                ]
            checked.append(
                {
                    "case": case,
                    "run": r["run_id"],
                    "raw_member_sha256": sha(raw),
                    "run_status": r["run"]["status"],
                    "failure_code": r["run"]["failure_code"],
                    "persisted_final_sha256": sha(final.encode()) if final else None,
                    "final_readback_matches_last_response": True if final else None,
                    "required_parameters": capabilities,
                    "proposals": proposals,
                    "admitted_business_observations": observations,
                    "admitted_policy_by_round": request_evidence,
                    "approval": {
                        k: approval[k]
                        for k in (
                            "decision_status",
                            "execution_status",
                            "execution_attempt_count",
                            "safe_result",
                        )
                    }
                    if approval
                    else None,
                    "tickets_before": (r.get("tickets_before") or {}).get("total"),
                    "tickets_after": (r.get("tickets_after") or {}).get(
                        "total", (r.get("reconciliation") or {}).get("postmortem_ticket_count")
                    ),
                    "wire_ids": [w["id"] for w in wires],
                    "wire_usage": [sum(w[k] for w in wires) for k in ("input", "output")],
                    "run_usage": usage,
                    "historical_task_pass": score["task_pass"],
                    "historical_contract_checks": score["contract_checks"],
                    "historical_claim_review": score["review"],
                    "limits": r.get("reconciliation"),
                }
            )
    assert len(checked) == 52 and sum(len(r["wire_ids"]) for r in checked) == len(receipts) == 129
    return {
        "source_sha256": manifest["artifact_sha256"]["refund-first-runs.zip"],
        "scope": "Prior judgments retained. Checked captures, receipts and persisted text.",
        "rows": checked,
    }


def inspect_contract(capture):
    report = json.loads((capture / "report.json").read_bytes())
    assert report["mode"] == "contract"
    rows = []
    with ZipFile(OLD_SCRIPTED) as previous:
        for item in report["rows"]:
            case = item["case_id"]
            raw = json.loads((capture / (case + ".json")).read_bytes())
            old = json.loads(previous.read(case + ".json"))
            version = report["groups"][raw["stage"]]["version"]
            assert (
                version["resolved_spec"]["runtime"]["support_answer_policy"] == "support_facts_v1"
            )
            assert raw["run"]["resolved_spec_hash"] == version["resolved_spec_hash"]
            assert (
                raw["contract_pass"]
                and raw["run"]["final_output"]
                == raw["turn"]["final_output"]
                == raw["calls"][-1]["response"]["content"]
            )
            increments = []
            projected = {}
            for msg in raw["calls"][-1]["request"]["messages"]:
                if msg["role"] != "tool" or msg["name"] == "search_knowledge":
                    continue
                payload = json.loads(msg["content"])
                projected[msg["name"]] = payload
                if msg["name"] in {"get_order", "get_refund_status", "query_customer"}:
                    assert payload["observation"]["scope"] == "this_tool_response"
            if case == "S0-036":
                for tool in ("get_order", "get_refund_status"):
                    assert projected[tool]["data"]["refund"] is None
                    assert projected[tool]["data"]["refund_requested_at"].startswith("2025-12-28")
                    assert projected[tool]["observation"]["refund_requested_at"] == "RETURNED"
            assert not raw["physical_attempts"]

            for c in raw["calls"]:
                req = c["request"]
                prompt = req["messages"][1]["content"]
                legacy = old["calls"][0]["request"]["messages"][1]["content"]
                assert prompt.startswith(legacy + "\n\nSupport facts v1.")
                assert json.loads(prompt.rsplit("\n", 1)[1]) and {
                    t["tool"] for t in json.loads(prompt.rsplit("\n", 1)[1])
                } == {t["name"] for t in req["tools"]}
                # Compare identical candidate request with only candidate annotations removed.
                # Use the pinned SDK serialization and tokenizer from dispatch preflight.
                stripped = deepcopy(c)
                stripped["request"]["messages"][1]["content"] = legacy
                for msg in stripped["request"]["messages"]:
                    if msg["role"] == "tool":
                        payload = json.loads(msg["content"])
                        if "observation" in payload:
                            del payload["observation"]
                            msg["content"] = json.dumps(
                                payload, sort_keys=True, separators=(",", ":")
                            )
                legacy_fixture = deepcopy(stripped)
                for msg in legacy_fixture["request"]["messages"]:
                    if msg["role"] == "tool" and msg["name"] == "get_refund_status":
                        payload = json.loads(msg["content"])
                        if "data" in payload:
                            payload["data"].pop("refund_requested_at", None)
                            payload["data"].setdefault("refund", None)
                            msg["content"] = json.dumps(
                                payload, sort_keys=True, separators=(",", ":")
                            )
                baseline, before, after = wire_bounds([legacy_fixture, stripped, c])
                increments.append(
                    {
                        "wire_tokens_with_template_headroom": after,
                        "annotation_token_delta": after - before,
                        "additional_refund_field_token_delta": before - baseline,
                    }
                )
            reference = old["visible"]
            new = raw["visible"]
            same = [(s["source_id"], sha(s["snippet"].encode())) for s in reference] == [
                (s["source_id"], sha(s["snippet"].encode())) for s in new
            ]
            dec = (raw.get("decision") or {}).get("approval") or {}
            rows.append(
                {
                    "case": case,
                    "run_id": raw["run_id"],
                    "version_hash": version["resolved_spec_hash"],
                    "contract_pass": True,
                    "persistent_answer_sha256": sha(raw["turn"]["final_output"].encode()),
                    "delivered_evidence": visible(raw["calls"][-1]["request"]["messages"]),
                    "same_ordered_policy_source_and_content_as_prior_script": same,
                    "old_snippets_sha256": [sha(s["snippet"].encode()) for s in reference],
                    "wire_rounds": increments,
                    "business_tool_messages": projected,
                    "required_support_groups_delivered": raw["coverage"],
                    "prior_required_support_groups_delivered": old["coverage"],
                    "approval": dec.get("decision_status"),
                    "execution": dec.get("execution_status"),
                    "tickets_before": raw["tickets_before"]["total"],
                    "tickets_after": raw["tickets_after"]["total"],
                }
            )
    assert len(rows) == 52
    return {
        "scope": "Scripted product Run and DB readback. No paid generation.",
        "physical_vendor_requests": 0,
        "provider_cost_cny": 0,
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path)
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    save(OUTPUT / "support-history.json", inspect_history())
    if args.capture:
        save(OUTPUT / "support-contract.json", inspect_contract(args.capture))


if __name__ == "__main__":
    main()
