"""Real process exits around simulated writes; never replay an uncertain action."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

from sqlalchemy import select

from benchmarks.evaluation.live_pilot import ISOLATED_URL
from packages.agent_runtime.adapters.langgraph import (
    LangGraphCheckpointAdapter,
    configure_windows_asyncio_policy,
)
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.approvals.contracts import ApprovalDecisionStatus
from packages.approvals.models import Approval
from packages.core.database import create_database
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.tools.actions import ActionExecutionResult, ActionRegistry, ActionRuntime
from tests.integration.test_m4c_agent_runtime import ScriptedGateway, _seed

WINDOWS = ("before_effect", "after_effect", "after_approval_commit")
CRASH_EXIT = 73


class CrashWrite:
    def __init__(self, ledger: Path, window: str, crash: bool):
        self.ledger, self.window, self.crash = ledger, window, crash
        with sqlite3.connect(ledger) as db:
            db.execute("CREATE TABLE IF NOT EXISTS dispatches (action_key TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS effects (action_key TEXT)")

    async def execute(self, context, definition, arguments, *, idempotency_key):
        with sqlite3.connect(self.ledger) as db:
            db.execute("INSERT INTO dispatches VALUES (?)", (idempotency_key,))
        if self.crash and self.window == "before_effect":
            os._exit(CRASH_EXIT)
        with sqlite3.connect(self.ledger) as db:
            db.execute("INSERT INTO effects VALUES (?)", (idempotency_key,))
        if self.crash and self.window == "after_effect":
            os._exit(CRASH_EXIT)
        return ActionExecutionResult.succeeded({"effect": "simulated_committed"})

    def counts(self):
        with sqlite3.connect(self.ledger) as db:
            return {
                "dispatch_count": db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0],
                "effect_count": db.execute("SELECT COUNT(*) FROM effects").fetchone()[0],
            }


class CrashApproval(ApprovalService):
    async def complete_execution(self, *args, **kwargs):
        await super().complete_execution(*args, **kwargs)
        os._exit(CRASH_EXIT)


def _runtime(factory, ledger, window, *, crash=False, gateway=None):
    executor = CrashWrite(ledger, window, crash)
    approval = (
        CrashApproval(factory)
        if crash and window == "after_approval_commit"
        else ApprovalService(factory)
    )
    return AgentRunService(
        factory,
        model_gateway_factory=lambda db: (
            gateway
            or ScriptedGateway([ModelResponse(content="done", provider="fake", model="fake")])
        ),
        approval_service=approval,
        action_runtime=ActionRuntime(
            session_factory=factory,
            registry=ActionRegistry(session_factory=factory, overrides={"create_ticket": executor}),
        ),
        checkpoint_adapter=LangGraphCheckpointAdapter(ISOLATED_URL),
    ), executor


async def seed(directory: Path, window: str):
    engine, factory = create_database(ISOLATED_URL)
    try:
        spec = {
            "kind": "builtin",
            "identity": "create_ticket",
            "description": "Simulated write",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_ref": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": ["customer_ref", "subject"],
                "additionalProperties": False,
            },
            "effect": "WRITE",
            "risk_level": "HIGH",
            "approval_policy": "ALWAYS",
        }
        async with factory() as db:
            base = await _seed(db, label=f"process-{uuid4().hex}", tool_spec=spec)
        context = base["context"].model_copy(
            update={
                "permissions": base["context"].permissions | {"approve_action", "workspace_read"}
            }
        )
        gateway = ScriptedGateway(
            [
                ModelResponse(
                    content="",
                    provider="fake",
                    model="fake",
                    tool_calls=(
                        ModelToolCall(
                            "create_ticket", {"customer_ref": "fictional", "subject": "isolated"}
                        ),
                    ),
                )
            ]
        )
        service, _ = _runtime(factory, directory / "effects.sqlite", window, gateway=gateway)
        first = await service.run(
            context, agent_version_id=base["version"].id, input_text="controlled action"
        )
        if first.status != "WAITING_APPROVAL":
            raise RuntimeError("SEED_DID_NOT_WAIT_FOR_APPROVAL")
        async with factory() as db:
            approval = await db.scalar(select(Approval).where(Approval.run_id == first.run_id))
        await ApprovalService(factory).decide(
            context, approval.id, decision=ApprovalDecisionStatus.APPROVED
        )
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "context": context.model_dump(mode="json"),
                    "run_id": str(first.run_id),
                    "approval_id": str(approval.id),
                    "window": window,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    finally:
        await engine.dispose()


async def child(directory: Path, phase: str):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    context = WorkspaceExecutionContext.model_validate(manifest["context"])
    engine, factory = create_database(ISOLATED_URL)
    try:
        service, executor = _runtime(
            factory, directory / "effects.sqlite", manifest["window"], crash=phase == "crash"
        )
        started = perf_counter()
        resumed = await service.resume(
            context, run_id=UUID(manifest["run_id"]), approval_id=UUID(manifest["approval_id"])
        )
        resume_ms = (perf_counter() - started) * 1000
        if phase == "crash":
            raise RuntimeError("CRASH_POINT_NOT_REACHED")
        # A second resume must not re-dispatch the logical action.
        await service.resume(
            context, run_id=UUID(manifest["run_id"]), approval_id=UUID(manifest["approval_id"])
        )
        approval = await ApprovalService(factory).get(context, UUID(manifest["approval_id"]))
        result = {
            "window": manifest["window"],
            "run_id": manifest["run_id"],
            "approval_id": manifest["approval_id"],
            "run_status": resumed.status,
            "failure_code": resumed.failure_code,
            "execution_status": approval.execution_status,
            "execution_attempt_count": approval.execution_attempt_count,
            "resume_ms": resume_ms,
            **executor.counts(),
        }
        (directory / "recovery.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        await engine.dispose()


def run(directory: Path, output: Path):
    directory.mkdir(parents=True, exist_ok=False)
    if output.exists():
        raise ValueError("OUTPUT_ALREADY_EXISTS")
    cases = []
    for window in WINDOWS:
        case_dir = directory / window
        case_dir.mkdir()
        asyncio.run(seed(case_dir, window))
        for phase, expected_exit in (("crash", CRASH_EXIT), ("recover", 0)):
            started = perf_counter()
            with (
                (case_dir / f"{phase}.stdout.log").open("wb") as out,
                (case_dir / f"{phase}.stderr.log").open("wb") as err,
            ):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "benchmarks.evaluation.process_recovery",
                        "--phase",
                        phase,
                        "--directory",
                        str(case_dir),
                    ],
                    stdout=out,
                    stderr=err,
                    timeout=60,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            if completed.returncode != expected_exit:
                raise RuntimeError(
                    f"{window}/{phase}: exit {completed.returncode}, expected {expected_exit}"
                )
            if phase == "recover":
                result = json.loads((case_dir / "recovery.json").read_text(encoding="utf-8"))
                result["restart_to_terminal_ms"] = (perf_counter() - started) * 1000
                result["crash_exit_code"] = CRASH_EXIT
                assert result["dispatch_count"] == result["execution_attempt_count"] == 1
                assert result["effect_count"] == (0 if window == "before_effect" else 1)
                assert result["run_status"] == (
                    "SUCCEEDED" if window == "after_approval_commit" else "NEEDS_ATTENTION"
                )
                cases.append(result)
    output.write_text(
        json.dumps(
            {
                "provider": "controlled",
                "side_effect": "durable_sqlite_simulation",
                "recovery_start": "immediately before launching a fresh recovery process",
                "timeout_seconds_per_process": 60,
                "cases": cases,
                "safe_terminal_count": sum(
                    c["run_status"] in {"SUCCEEDED", "NEEDS_ATTENTION"} for c in cases
                ),
                "business_recovered_count": sum(c["run_status"] == "SUCCEEDED" for c in cases),
                "planned_count": len(WINDOWS),
                "boundary": (
                    "CLAIMED without a recorded outcome requires operator reconciliation; "
                    "no automatic replay"
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("run", "crash", "recover"), default="run")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    configure_windows_asyncio_policy()
    if args.phase == "run":
        if args.output is None:
            parser.error("--output is required")
        run(args.directory, args.output)
    else:
        asyncio.run(child(args.directory, args.phase))


if __name__ == "__main__":
    main()

