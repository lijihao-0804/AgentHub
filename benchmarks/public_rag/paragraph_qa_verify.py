"""Check recorded answer scores, visible source text and persisted usage offline."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from collections import Counter
from pathlib import Path
from uuid import UUID

from benchmarks.public_rag.paragraph_qa_audit import read
from benchmarks.public_rag.paragraph_qa_live import ARMS, ROOT
from benchmarks.public_rag.scenario_runner import dump
from packages.agent_runtime.models import AgentRun
from packages.core.database import create_database


async def verify(run_name):
    folder = ROOT / run_name
    data, state = read(ROOT / "frozen-dataset.json"), read(ROOT / "prepared.json")
    sources = read(ROOT / "source-map.json")
    rows, report = read(folder / "run.json"), read(folder / "report.json")
    done = read(folder / "completed.json")
    cases = {c["query_id"]: c for c in data["cases"]}
    scored = {c["query_id"]: c for c in report["cases"]}
    official_path = Path("data/external/rag-public-20261021/qasper/extracted/qasper_evaluator.py")
    spec = importlib.util.spec_from_file_location("official_qasper", official_path)
    official = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(official)
    assert len(rows) == len(cases) == 70 and set(scored) == set(cases)
    engine, factory = create_database(state["infrastructure"]["database_url"])
    counts, models, usage = Counter(), Counter(), Counter()
    visibility = {arm: Counter() for arm in ARMS}
    seen_runs = set()
    arm_f1, arm_usage = {a: [] for a in ARMS}, {a: Counter() for a in ARMS}
    try:
        async with factory() as db:
            for row in rows:
                case = cases[row["query_id"]]
                for arm, result in row["arms"].items():
                    quality = scored[row["query_id"]]["arms"][arm]
                    answer = result["answer"]
                    prediction = (
                        ("Unanswerable" if quality["refused"] else answer["answer"])
                        if quality["valid_response"]
                        else ""
                    )
                    expected = max(
                        official.token_f1_score(
                            prediction,
                            "Unanswerable" if a["answer_type"] == "unanswerable" else a["answer"],
                        )
                        for a in case["annotations"]
                    )
                    assert abs(expected - quality["token_f1"]) < 1e-12
                    counts["official_f1_parity"] += 1
                    arm_f1[arm].append(expected)
                    for record in result["visible_records"]:
                        source = sources[record["chunk_id"]]
                        assert source["paper_id"] == case["paper_id"] and source["arm"] == arm
                        assert record["snippet"] in source["chunk_text"]
                        counts["exact_source_substrings"] += 1
                        visibility[arm]["visible_records"] += 1
                        visibility[arm]["visible_characters"] += len(record["snippet"])
                        visibility[arm]["complete_indexed_chunk"] += (
                            record["snippet"] == source["chunk_text"]
                        )
                    for call in result["physical_outcomes"]:
                        models[call.get("raw_response_model", call["status"])] += 1
                    if result["run_id"] is None:
                        counts["synthetic_missing_arms"] += 1
                        continue
                    assert result["run_id"] not in seen_runs
                    seen_runs.add(result["run_id"])
                    stored = await db.get(AgentRun, UUID(result["run_id"]))
                    assert stored.status == result["run_status"]
                    assert stored.agent_version_id == UUID(result["agent_version_id"])
                    assert stored.input_text == case["question"]
                    assert stored.tool_call_count == result["tool_calls"]
                    for field in ("input_tokens", "output_tokens", "total_tokens"):
                        observed = result["run_usage"][field]
                        assert getattr(stored, "total_" + field.removeprefix("total_")) == observed
                        if observed is not None:
                            usage[field] += observed
                            arm_usage[arm][field] += observed
                    counts["persisted_runs_verified"] += 1
    finally:
        await engine.dispose()
    assert counts["persisted_runs_verified"] == done["executed_runs"]
    for arm in ARMS:
        assert abs(sum(arm_f1[arm]) / 70 - report["arms"][arm]["primary"]["token_f1"]) < 1e-12
        for field, value in arm_usage[arm].items():
            assert value == report["arms"][arm]["primary"]["run_usage"][field]["sum"]
    if done.get("remainder_execution_complete"):
        amendment = read(ROOT / "operational-amendment.json")
        assert len(seen_runs) == amendment["new_runs"] == 138
        skipped = set(amendment["skip_entire_queries"])
        assert len(skipped) == 1 and done["skipped_exposed_queries"] == sorted(skipped)
        for row in rows:
            assert all(a["run_id"] is None for a in row["arms"].values()) == (
                row["query_id"] in skipped
            )
        original = read(ROOT / amendment["original_run"] / "run.json")
        original_ids = {a["run_id"] for r in original for a in r["arms"].values() if a["run_id"]}
        assert not seen_runs.intersection(original_ids)
    dump(
        folder / "verification-v2.json",
        {
            "status": "PASS_RECORDED_CHECKS_ONLY",
            "original_plan_completed": done.get("full_original_execution_complete", False),
            "remainder_execution_complete": done.get("remainder_execution_complete", False),
            "checks": dict(counts),
            "actual_raw_models": dict(models),
            "persisted_observed_usage": dict(usage),
            "visibility": visibility,
            "scope": (
                "Official lexical F1 parity, exact source ownership/substrings and SQL Run usage; "
                "not semantic support or transport delivery proof."
            ),
        },
    )
    print(dict(counts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-name", default="live-run-1")
    asyncio.run(verify(parser.parse_args().run_name))
