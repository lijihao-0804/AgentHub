"""Real provider + real loopback HTTP TTFT, isolated fixtures and cumulative budget."""

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select

from benchmarks.evaluation.artifacts import write_json_atomic
from benchmarks.evaluation.budget import TrialBudget
from benchmarks.evaluation.formal_support import ROOT, assert_committed_code, context_for
from benchmarks.evaluation.http_stream_probe import frames, server_for
from benchmarks.evaluation.live_pilot import ISOLATED_URL, PRICE_ID
from benchmarks.evaluation.stream_budget import BudgetedStreamingAdapter
from packages.agent_runtime.adapters.langgraph import (
    LangGraphCheckpointAdapter,
    configure_windows_asyncio_policy,
)
from packages.agent_runtime.models import AgentVersion
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer


async def run(plan: Path, output: Path):
    assert_committed_code()
    if output.exists():
        raise ValueError("STREAM_REPORT_ALREADY_EXISTS")
    frozen = json.loads(plan.read_text(encoding="utf-8"))
    engine, factory = create_database(ISOLATED_URL)
    budget = TrialBudget(
        ROOT / "live-budget.json", limit_cny=Decimal("50"), price_identity=PRICE_ID
    )
    adapter = BudgetedStreamingAdapter(budget)
    cipher = ProviderCredentialCipher.from_settings(Settings())
    runtime = AgentRunService(
        factory,
        model_gateway_factory=lambda db: SqlAlchemyModelGateway(
            db, adapter=adapter, credential_cipher=cipher
        ),
        approval_service=ApprovalService(factory),
        action_runtime=ActionRuntime(session_factory=factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(ISOLATED_URL),
    )
    report = {
        "status": "RUNNING",
        "provider": "deepseek-flash",
        "transport": "real_loopback_http_sse",
        "billing_method": "peak_upper_cny_not_invoice",
        "cases": [],
        "limitations": [
            "fixture_identity_injected_not_login_test",
            "small_sample_not_SLA",
            "approval_wait_not_included_in_direct_answer_group",
        ],
    }
    try:
        async with factory() as session:
            source = await session.get(AgentVersion, UUID(frozen["agent_version_ids"][1]))
            if source is None:
                raise ValueError("STREAM_SOURCE_VERSION_MISSING")
            spec = json.loads(json.dumps(source.resolved_spec))
            spec["model"]["capabilities"]["streaming"] = True
            number = await session.scalar(
                select(func.max(AgentVersion.version_number)).where(
                    AgentVersion.agent_id == source.agent_id
                )
            )
            version = AgentVersion(
                workspace_id=source.workspace_id,
                agent_id=source.agent_id,
                version_number=number + 1,
                spec_schema_version=source.spec_schema_version,
                resolved_spec=spec,
                resolved_spec_hash=canonical_json_hash(spec),
                created_by=source.created_by,
            )
            session.add(version)
            await session.commit()
        context = await context_for(factory, source.created_by, source.workspace_id)
        from benchmarks.evaluation.reviewed_support_data import reviewed_source

        sources = reviewed_source()
        cases = [
            c for c in sources["cases"] if c["split"] == "DEV" and c["category"] == "single_hop"
        ]
        tasks = [
            ("direct_answer", c["case_id"], c["user_turns"][0]) for c in cases for _ in range(3)
        ]
        tasks += [
            (
                "approval_wait",
                "approval-stream",
                "为客户 fixture_customer_ref 登记退款待处理工单，先查询客户并提交创建审批。",
            )
            for _ in range(3)
        ]
        timings = {}
        report.update(
            agent_version_id=str(version.id),
            spec_hash=version.resolved_spec_hash,
            planned_case_count=len(tasks),
        )
        async with server_for(factory, runtime, context, timings) as url:
            async with httpx.AsyncClient(base_url=url, timeout=90) as client:
                for ordinal, (group, case_id, text) in enumerate(tasks):
                    if adapter.halted:
                        raise ValueError("STREAM_PROVIDER_HALTED")
                    token = uuid4().hex
                    if group == "approval_wait":
                        async with factory() as session:
                            customer = Customer(
                                workspace_id=source.workspace_id,
                                customer_ref=f"stream-{uuid4().hex}",
                                name="synthetic stream fixture",
                            )
                            session.add(customer)
                            await session.commit()
                        text = text.replace("fixture_customer_ref", customer.customer_ref)
                    started, first_text = perf_counter(), None
                    observed = []
                    first_call = len(adapter.calls)
                    async with client.stream(
                        "POST",
                        f"/api/v1/workspaces/{source.workspace_id}/agent-versions/{version.id}/runs/stream",
                        json={"input_text": text},
                        headers={"x-probe-id": token},
                    ) as response:
                        async for frame in frames(response):
                            observed.append(frame)
                            if (
                                first_text is None
                                and frame["type"] == "message.delta"
                                and frame["payload"].get("delta")
                            ):
                                first_text = (perf_counter() - started) * 1000
                    elapsed = (perf_counter() - started) * 1000
                    run_id = observed[0]["run_id"]
                    persisted = await runtime.get_run(context, UUID(run_id))
                    # HTTP termination arrives before middleware finally returns;
                    # yield to its lifecycle once, then retain absence as unknown.
                    await asyncio.sleep(0)
                    report["cases"].append(
                        {
                            "ordinal": ordinal,
                            "group": group,
                            "case_id": case_id,
                            "run_id": run_id,
                            "status": persisted.status,
                            "failure_code": persisted.failure_code,
                            "client_ttft_ms": first_text,
                            "client_ttft_missing_reason": "no_visible_text"
                            if first_text is None
                            else None,
                            "http_duration_ms": elapsed,
                            "server_timing": timings.get(token),
                            "event_types": [f["type"] for f in observed],
                            "attempt_ids": [c["attempt_id"] for c in adapter.calls[first_call:]],
                        }
                    )
                    report.update(calls=adapter.calls, allocated_cny=str(budget.allocated))
                    write_json_atomic(output, report)
        report["status"] = "EXECUTED"
    except Exception as error:
        report.update(status="STOPPED", failure_class=type(error).__name__, failure_code=str(error))
        raise
    finally:
        report.update(calls=adapter.calls, allocated_cny=str(budget.allocated))
        write_json_atomic(output, report)
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    configure_windows_asyncio_policy()
    asyncio.run(run(args.plan, args.output))
