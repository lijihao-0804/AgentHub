import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from benchmarks.evaluation.artifacts import write_json_atomic
from benchmarks.evaluation.live_pilot import ISOLATED_URL
from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.agent_runtime.models import RunStep
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database


async def main(path):
    data = json.loads(path.read_text(encoding="utf8"))
    if data["status"] != "SUCCEEDED":
        raise ValueError("RUN_NOT_COMPLETE")
    engine, factory = create_database(ISOLATED_URL)
    rows = []
    try:
        async with factory() as s:
            for case in data["cases"]:
                ids = [UUID(t["run_id"]) for t in case["observation"]["turns"]]
                steps = list(
                    await s.scalars(
                        select(RunStep)
                        .where(
                            RunStep.workspace_id == UUID(data["plan"]["workspace_id"]),
                            RunStep.agent_run_id.in_(ids),
                            RunStep.kind == "MODEL",
                        )
                        .order_by(RunStep.agent_run_id, RunStep.sequence_number)
                    )
                )
                m = [step.safe_metadata for step in steps]
                known = all(
                    step.status == "SUCCEEDED"
                    and v.get("cost_amount") is not None
                    and v.get("cost_currency") == "CNY"
                    for step, v in zip(steps, m, strict=True)
                )
                total = (
                    sum((Decimal(str(v["cost_amount"])) for v in m), Decimal(0)) if known else None
                )
                rows.append(
                    {
                        "case_result_id": case["id"],
                        "case_id": case["case_id"],
                        "variant_id": case["variant_id"],
                        "repetition": case["repetition"],
                        "step_ids": [str(step.id) for step in steps],
                        "step_metadata_hash": canonical_json_hash(m),
                        "model_step_count": len(m),
                        "cost_amount": str(total) if total is not None else None,
                        "cost_currency": "CNY" if known else None,
                        "runtime_reported_cost_amount": case["cost_amount"],
                        "input_tokens": sum(v["input_tokens"] for v in m)
                        if all(v.get("input_tokens") is not None for v in m)
                        else None,
                        "output_tokens": sum(v["output_tokens"] for v in m)
                        if all(v.get("output_tokens") is not None for v in m)
                        else None,
                    }
                )
        expected = sum(Decimal(c["upper_estimated_cost_cny"]) for c in data["calls"])
        actual = sum(
            (Decimal(r["cost_amount"]) for r in rows if r["cost_amount"] is not None), Decimal(0)
        )
        result = {
            "version": "persisted-model-step-cost-reconciliation-v1",
            "raw_report_hash": canonical_json_hash(data),
            "source": "workspace_scoped_run_steps_model_metadata",
            "rows": rows,
            "persisted_model_step_count": sum(r["model_step_count"] for r in rows),
            "adapter_call_count": len(data["calls"]),
            "persisted_step_total_cny": str(actual),
            "adapter_total_cny": str(expected),
            "status": "PASS"
            if actual == expected
            and all(r["cost_amount"] is not None for r in rows)
            and sum(r["model_step_count"] for r in rows) == len(data["calls"])
            else "FAIL",
            "runtime_cost_understatement_cny": str(
                actual - sum(Decimal(c["cost_amount"]) for c in data["cases"])
            ),
            "history_policy": "raw experiment observations preserved; supplemental accounting only",
        }
        out = path.with_suffix(".cost-reconciliation.json")
        write_json_atomic(out, result)
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    configure_windows_asyncio_policy()
    asyncio.run(main(Path(sys.argv[1])))
