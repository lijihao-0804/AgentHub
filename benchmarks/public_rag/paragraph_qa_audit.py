"""Persisted development-run checks and preregistered acceptance, without API calls."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select

from benchmarks.public_rag.paragraph_qa_live import ARMS, PROMPT, RETRIEVAL, ROOT
from benchmarks.public_rag.scenario_runner import dump
from packages.agent_runtime.models import AgentVersion
from packages.core.database import create_database
from packages.knowledge.models import DocumentChunk, DocumentRevision, IngestionJob


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


async def preflight():
    state = read(ROOT / "prepared.json")
    sources = read(ROOT / "source-map.json")
    engine, factory = create_database(state["infrastructure"]["database_url"])
    totals = Counter()
    try:
        async with factory() as db:
            for paper, arms in state["papers"].items():
                for arm, info in arms.items():
                    job = await db.get(IngestionJob, UUID(info["job_id"]))
                    revision = await db.get(DocumentRevision, UUID(info["revision_id"]))
                    assert job.status == "SUCCEEDED" and revision.ingestion_status == "READY"
                    chunks = list(
                        await db.scalars(
                            select(DocumentChunk).where(
                                DocumentChunk.document_revision_id == revision.id
                            )
                        )
                    )
                    assert len(chunks) == info["chunks"]
                    for chunk in chunks:
                        source = sources[chunk.chunk_id]
                        assert source["paper_id"] == paper and source["arm"] == arm
                        assert source["chunk_text"] == chunk.text
                        assert arm != "structural" or len(chunk.text) <= 1000
                    version = await db.get(
                        AgentVersion, UUID(state["versions"][paper][arm]["version_id"])
                    )
                    spec = version.resolved_spec
                    assert spec == state["versions"][paper][arm]["resolved_spec"]
                    assert spec["prompt"]["system_prompt"] == PROMPT
                    model = spec["model"]
                    assert model["model"] == "auto/deepseek-v4.1-flash"
                    assert model["max_tokens"] == 1024 and model["temperature"] == 0
                    assert model["retry_policy"]["max_attempts"] == 1
                    backup = model["fallback_profiles"]
                    assert len(backup) == 1
                    assert backup[0]["model"] == "deepseek-v4.1-flash-1017"
                    assert backup[0]["max_tokens"] == 1024
                    assert all(spec["retrieval"][k] == v for k, v in RETRIEVAL.items())
                    binding = spec["retrieval"]["knowledge_bindings"][0]
                    assert binding["knowledge_base_id"] == info["knowledge_base_id"]
                    assert binding["snapshot_id"] == info["snapshot_id"]
                    assert spec["runtime"]["max_tool_calls"] == 1
                    assert spec["runtime"]["context_budget"]["max_retrieval_tokens"] == 8000
                    totals[arm] += len(chunks)
    finally:
        await engine.dispose()
    assert dict(totals) == {"fixed": 635, "structural": 1558}
    dump(
        ROOT / "persisted-preflight.json",
        {
            "status": "PASS",
            "published_versions": 48,
            "successful_ingestion_jobs": 48,
            "actual_chunks": dict(totals),
            "source_entries": len(sources),
            "same_prompt_route_budget_and_pinned_snapshots_verified": True,
            "scope": "Persisted DB and source-text checks; no paid request or hash assertion.",
        },
    )
    print(json.dumps({"persisted_preflight": "PASS", "chunks": dict(totals)}))


def freeze_sources():
    target = ROOT / "execution-sources.zip"
    if target.exists():
        raise ValueError("execution source archive already exists")
    paths = [
        *Path("benchmarks/public_rag").glob("paragraph_qa*.py"),
        *Path("benchmarks/public_rag").glob("paper_qa*.py"),
        Path("benchmarks/public_rag/consumer_answer_metrics.py"),
        Path("benchmarks/public_rag/consumer_answer_client.py"),
        Path("benchmarks/public_rag/consumer_replay.py"),
        Path("benchmarks/public_rag/scenario_runner.py"),
        Path("benchmarks/public_rag/model-route.json"),
        Path("packages/knowledge/structural_chunking.py"),
        Path("packages/knowledge/chunking.py"),
        Path("packages/knowledge/evidence_records.py"),
        Path("packages/model_gateway/configured_route.py"),
        Path("scripts/model_route.py"),
        Path("apps/worker/tasks/knowledge.py"),
        ROOT / "protocol.json",
        ROOT / "sampling-audit.json",
    ]
    with ZipFile(target, "x", compression=ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.as_posix())
    print(json.dumps({"execution_sources_archived": len(paths)}))


def acceptance(run_name):
    folder = ROOT / run_name
    report, done = read(folder / "report.json"), read(folder / "completed.json")
    protocol = read(ROOT / "protocol.json")
    gates = protocol["usefulness_gates"]
    arms = {arm: report["arms"][arm]["primary"] for arm in ARMS}
    base, candidate = arms["fixed"], arms["structural"]
    ci = report["paired_95ci"]["answer_token_f1"]

    def ratio(field):
        left, right = base[field], candidate[field]
        if field == "end_to_end_latency_ms":
            left, right = left["p95"], right["p95"]
        else:
            left, right = left["input_tokens"]["sum"], right["input_tokens"]["sum"]
        return right / left if left and right is not None else None

    latency_ratio, input_ratio = ratio("end_to_end_latency_ms"), ratio("model_usage")
    negative = {a: report["arms"][a]["strata"]["unanswerable"] for a in ARMS}
    checks = {
        "complete": done["run_state"] == "COMPLETE" and done["executed_runs"] == 140,
        "gain_at_least_5pp": ci.get("difference", 0) >= gates["minimum_f1_gain"],
        "positive_paper_clustered_ci": ci.get("ci_low", 0) > 0
        and ci["groups"] == 24
        and ci["inference_valid"],
        "em_no_loss": candidate["exact_match"] >= base["exact_match"],
        "valid_responses": all(a["valid_response_fraction"] >= 0.95 for a in arms.values()),
        "unanswerable_no_more_unsupported": negative["structural"]["unsupported_answer_rate"]
        <= negative["fixed"]["unsupported_answer_rate"],
        "warm_p95": latency_ratio is not None and latency_ratio <= gates["maximum_warm_p95_ratio"],
        "input_tokens": input_ratio is not None
        and input_ratio <= gates["maximum_input_tokens_ratio"],
        "primary_only_confirmed": done["fallback_attempts"] == 0
        and done["physical_outcomes"]["TRANSPORT_UNCONFIRMED"] == 0
        and done["physical_outcomes"]["MODEL_IDENTITY_ERROR"] == 0
        and done["stop_reason"] is None
        and done.get("continuation_from") is None,
    }
    result = {
        "status": "PASS_DEVELOPMENT_ONLY" if all(checks.values()) else "NOT_PROMOTED",
        "checks": checks,
        "f1": {a: arms[a]["token_f1"] for a in ARMS},
        "paired_f1": ci,
        "warm_p95_ratio": latency_ratio,
        "input_tokens_ratio": input_ratio,
        "scope": (
            "Original 70-question plan with zero-padded exposed pair; "
            "operational remainder is descriptive only, no independent holdout claim."
            if done.get("continuation_from")
            else "Preregistered 24-paper TRAIN development; no independent holdout claim."
        ),
        "default_changed": False,
        "currency_cost": None,
        "completed": done,
    }
    dump(ROOT / "acceptance.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "freeze", "acceptance"))
    parser.add_argument("--run-name", default="live-run-1")
    args = parser.parse_args()
    if args.phase == "preflight":
        asyncio.run(preflight())
    elif args.phase == "freeze":
        freeze_sources()
    else:
        acceptance(args.run_name)
