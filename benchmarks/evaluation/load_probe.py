"""Bounded closed-loop runtime probe with a controlled provider, not model QPS."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import tracemalloc
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from sqlalchemy import func, select

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.core.database import create_database
from packages.evaluation.metrics import percentile
from packages.model_gateway.contracts import ModelResponse
from tests.integration.test_m4c_agent_runtime import ScriptedGateway, _seed


async def measure_phase(
    operation: Callable[[], Awaitable[dict]],
    *,
    concurrency: int,
    duration: float,
    request_timeout: float = 5,
    per_worker_rate: float = 2,
) -> dict:
    if (
        type(concurrency) is not int
        or concurrency not in {1, 4, 8}
        or not 0 < duration <= 60
        or not 0 < request_timeout <= 5
        or not 0 < per_worker_rate <= 2
    ):
        raise ValueError("PROBE_RESOURCE_LIMIT_EXCEEDED")
    started, cpu_start = perf_counter(), os.times()
    if tracemalloc.is_tracing():
        tracemalloc.reset_peak()
    deadline, records = started + duration, []

    async def worker():
        while perf_counter() < deadline:
            attempt_started = perf_counter()
            try:
                outcome = await asyncio.wait_for(operation(), request_timeout)
            except TimeoutError:
                outcome = {"status": "UNKNOWN", "failure_code": "PROBE_TIMEOUT"}
            except Exception as exc:
                outcome = {"status": "UNKNOWN", "failure_code": type(exc).__name__}
            records.append({**outcome, "latency_ms": (perf_counter() - attempt_started) * 1000})
            pause = min(
                deadline - perf_counter(),
                max(0, 1 / per_worker_rate - (perf_counter() - attempt_started)),
            )
            if pause > 0:
                await asyncio.sleep(pause)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed, cpu_end = perf_counter() - started, os.times()
    completed = sum(r.get("status") == "SUCCEEDED" for r in records)
    latencies = [r["latency_ms"] for r in records]
    return {
        "concurrency": concurrency,
        "target_window_seconds": duration,
        "actual_window_seconds": elapsed,
        "attempt_count": len(records),
        "successful_count": completed,
        "failure_or_unknown_count": len(records) - completed,
        "success_per_second": completed / elapsed,
        "failure_or_unknown_rate": (len(records) - completed) / len(records) if records else None,
        "latency_p50_ms": percentile(latencies, 0.5) if latencies else None,
        "latency_p95_ms": percentile(latencies, 0.95) if latencies else None,
        "process_cpu_seconds": (cpu_end.user + cpu_end.system)
        - (cpu_start.user + cpu_start.system),
        "python_traced_heap_bytes": dict(
            zip(("current", "phase_peak"), tracemalloc.get_traced_memory(), strict=True)
        )
        if tracemalloc.is_tracing()
        else None,
        "failure_codes": dict(
            Counter(r.get("failure_code") for r in records if r.get("failure_code"))
        ),
        "queue": {"status": "NOT_APPLICABLE", "reason": "closed_loop_no_celery_queue"},
        "offered_load": {
            "per_worker_max_requests_per_second": per_worker_rate,
            "capacity_saturation_claimed": False,
        },
        "records": records,
    }


async def run_probe(database_url: str, *, duration: float, output: Path) -> dict:
    # This runner's writes must never target the normal application database.
    if database_url.rsplit("/", 1)[-1] != "agenthub_mi34_20261002":
        raise ValueError("ISOLATED_PROBE_DATABASE_REQUIRED")
    engine, factory = create_database(database_url)
    tracing_owned = not tracemalloc.is_tracing()
    if tracing_owned:
        tracemalloc.start()
    try:
        async with factory() as session:
            fixture = await _seed(session, label=f"controlled-load-{uuid4().hex}")
        runtime = AgentRunService(
            factory,
            model_gateway_factory=lambda session: ScriptedGateway(
                [ModelResponse(content="controlled probe", provider="fake", model="frozen-model-a")]
            ),
        )

        async def operation():
            result = await runtime.run(
                fixture["context"],
                agent_version_id=fixture["version"].id,
                input_text="Controlled READ-only runtime probe.",
            )
            return {
                "run_id": str(result.run_id),
                "status": result.status,
                "failure_code": result.failure_code,
            }

        warmups = []
        for label in ("process_first_runtime_run", "immediate_warm_runtime_run"):
            started = perf_counter()
            result = await asyncio.wait_for(operation(), 5)
            warmups.append(
                {"label": label, **result, "latency_ms": (perf_counter() - started) * 1000}
            )
        phases = []
        for concurrency in (1, 4, 8):
            phases.append(
                await measure_phase(operation, concurrency=concurrency, duration=duration)
            )
        async with factory() as session:
            persisted = dict(
                (
                    await session.execute(
                        select(AgentRun.status, func.count())
                        .where(AgentRun.workspace_id == fixture["workspace_id"])
                        .group_by(AgentRun.status)
                    )
                ).all()
            )
        expected = 2 + sum(phase["attempt_count"] for phase in phases)
        report = {
            "schema_version": 1,
            "driver_kind": "controlled",
            "provider_kind": "in_process_scripted",
            "build_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "working_tree_changes_present": bool(
                subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
            ),
            "agent_version_id": str(fixture["version"].id),
            "resolved_spec_hash": fixture["version"].resolved_spec_hash,
            "workspace_id": str(fixture["workspace_id"]),
            "environment": {
                "os": platform.platform(),
                "python": platform.python_version(),
                "logical_cpu_count": os.cpu_count(),
                "database": "isolated_database_shared_local_postgres",
            },
            "limitations": [
                "not_real_provider_qps",
                "not_http_sse_client_latency",
                "no_model_weights_cold_start",
                "rate_capped_not_maximum_capacity",
                "resource_cpu_is_python_process_only",
                "heap_is_python_allocations_not_process_rss",
                "container_resources_not_collected",
            ],
            "warmups": warmups,
            "phases": phases,
            "persistent_status_counts": persisted,
            "expected_persisted_run_count": expected,
            "persistence_count_matches": sum(persisted.values()) == expected,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if not report["persistence_count_matches"] or any(
            p["failure_or_unknown_count"] for p in phases
        ):
            raise RuntimeError("PROBE_FAILED_INSPECT_PERSISTENT_REPORT")
        return report
    finally:
        await engine.dispose()
        if tracing_owned:
            tracemalloc.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.duration <= 60:
        parser.error("duration must be at most 60 seconds per phase")
    report = asyncio.run(run_probe(args.database_url, duration=args.duration, output=args.output))
    print(
        json.dumps(
            {
                "persistence_count_matches": report["persistence_count_matches"],
                "phases": [
                    {k: v for k, v in p.items() if k != "records"} for p in report["phases"]
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
