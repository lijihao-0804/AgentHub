"""Read-only subprocess probe importing the actual c224 checkout, not new ORM."""

import asyncio
import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from apps.api.schemas.agent_runs import RunStepResponse
from packages.agent_runtime import models
from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.agent_runtime.models import AgentRun, RunStep
from packages.core.database import create_database


async def main():
    expected = "c2240b1b06be4ab4a19f9a60a372a16b1d1c49d4"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    assert commit == expected
    assert Path(models.__file__).resolve().is_relative_to(Path.cwd().resolve())
    url = make_url(os.environ["AGENTHUB_READBACK_DATABASE_URL"])
    assert url.host == "127.0.0.1" and url.database.startswith("agenthub_step_accept_")
    engine, factory = create_database(url.render_as_string(hide_password=False))
    try:
        async with factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            rows = list(
                await session.scalars(
                    select(RunStep).where(
                        RunStep.kind.in_(("EVIDENCE_SELECTION", "AUXILIARY_MODEL"))
                    )
                )
            )
            payloads = [RunStepResponse.model_validate(row).model_dump() for row in rows]
            runs = list(
                await session.scalars(
                    select(AgentRun).where(
                        AgentRun.id.in_(
                            {row.agent_run_id for row in rows if row.kind == "AUXILIARY_MODEL"}
                        )
                    )
                )
            )
            assert len(runs) == 2
            for run in runs:
                assert run.status == "SUCCEEDED" and run.total_tokens == 38
                assert run.total_input_tokens == 33 and run.total_output_tokens == 5
                assert run.total_cost_amount == Decimal(".0003") and run.cost_currency == "USD"
            print(
                json.dumps(
                    {
                        "commit": commit,
                        "transaction": "READ ONLY",
                        "auxiliary_rows": sum(p["kind"] == "AUXILIARY_MODEL" for p in payloads),
                        "selection_rows": sum(p["kind"] == "EVIDENCE_SELECTION" for p in payloads),
                        "completed_usage_runs": len(runs),
                        "provider_requests": 0,
                    }
                )
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    configure_windows_asyncio_policy()
    asyncio.run(main())
