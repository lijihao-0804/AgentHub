"""Exercise both product arms on a previously exposed ticket with a scripted gateway."""

import argparse
import asyncio
import json
import os
import zipfile
from pathlib import Path

from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy

from .answer_contract_pair import digest, run_case
from .product_acceptance import ContractGateway
from .runtime_trial import ROOT, read, save


async def smoke(output):
    cases = read(ROOT / "new_ticket_cases.json")["cases"]
    docs = [
        dict(source_id=c["source_id"], source=c["source"], effective_from=date)
        for c, date in zip(cases, ["2026-10-01", "2026-09-15"], strict=True)
    ]
    case = dict(
        cases[0], documents=docs, order=None, required_reads=["search_knowledge", "query_customer"]
    )
    records = []
    for arm in ("baseline", "candidate"):
        record = await run_case(
            case, arm, output=output / arm, meter=None, scripted_gateway=ContractGateway()
        )
        assert record["terminal_known"] and record["run"]["status"] == "SUCCEEDED"
        assert len(record["ticket_details"]) == 1 and record["tickets_before"]["total"] == 0
        assert record["ticket_details"][0]["ticket_ref"] in record["turn"]["final_output"]
        assert record["request_audit"][-1]["evidence"]
        records.append(read(output / arm / "run.json"))
    assert records[0]["database"] != records[1]["database"]
    assert records[0]["policy_content_sha256"] == records[1]["policy_content_sha256"]
    save(output / "paired-smoke.json", compare_archive(records, case["case_id"]))


def compare_archive(records, case_id):
    archive = Path(
        "benchmarks/public_rag/results/published/rag-product-evidence-20261114/paid/refund-first-runs.zip"
    )
    with zipfile.ZipFile(archive) as z:
        previous = json.loads(z.read(case_id + ".json"))
    assert records[0]["calls"][0]["request"] == previous["calls"][0]["request"]
    assert records[1]["calls"][0]["request"]["tools"] == previous["calls"][0]["request"]["tools"]
    assert records[1]["request_audit"][0]["support_system_extension_tokens"] > 0
    for r, arm in zip(records, ["baseline", "candidate"], strict=True):
        print(arm, r["run_id"], r["run"]["status"], len(r["calls"]))
    return {
        "case_id": case_id,
        "scope": "previously exposed development ticket; scripted generator",
        "provider_requests": 0,
        "provider_cost_cny": 0,
        "reproduce": (
            "python -m benchmarks.evaluation.experiments.support_refund_rag.answer_pair_smoke "
            "--output .scratch/support-pair-fresh"
        ),
        "historical_archive": str(archive.as_posix()),
        "historical_member": case_id + ".json",
        "model_request_hash_encoding": "SHA-256 of UTF-8 sorted compact JSON, ensure_ascii=False",
        "historical_first_request_sha256": digest(previous["calls"][0]["request"]),
        "baseline_entire_first_model_request_equal": True,
        "arms": [
            {
                "arm": r["arm"],
                "run_id": r["run_id"],
                "database": r["database"],
                "workspace": r["workspace"],
                "status": r["run"]["status"],
                "thread_id": r["thread_id"],
                "policy_content_sha256": r["policy_content_sha256"],
                "first_model_request_sha256": digest(r["calls"][0]["request"]),
                "first_model_request": r["calls"][0]["request"],
                "request_audit": r["request_audit"],
                "terminal_known": r["terminal_known"],
                "ticket_requests": r["turn"]["ticket_requests"],
                "ticket_readback": r["ticket_details"],
                "tickets_before": r["tickets_before"],
                "persisted_first_answer": r["turn"]["final_output"],
                "scripted_model_steps": len(r["calls"]),
            }
            for r in records
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    configure_windows_asyncio_policy()
    asyncio.run(smoke(parser.parse_args().output))
