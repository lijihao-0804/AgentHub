"""Controlled side effects in a separate durable SQLite ledger, real PG approvals."""

import json
import os
import sqlite3
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import select

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.approvals.contracts import ApprovalDecisionStatus
from packages.approvals.models import Approval
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.tools.actions import ActionExecutionResult, ActionRegistry, ActionRuntime
from tests.integration.test_m4c_agent_runtime import ScriptedGateway, _seed
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database

__all__ = ["db_factory", "migrated_database"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("AGENTHUB_TEST_DATABASE_URL"), reason="PostgreSQL required"),
]


class DurableSimulatedWrite:
    def __init__(self, path, mode):
        self.path, self.mode = path, mode
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS effects (action_key TEXT, outcome TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS dispatches (action_key TEXT)")

    async def execute(self, context, definition, arguments, *, idempotency_key):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO dispatches VALUES (?)", (idempotency_key,))
            if self.mode == "definite_failure":
                return ActionExecutionResult.failed("SIMULATED_REJECTED", "Rejected before effect.")
            db.execute("INSERT INTO effects VALUES (?, ?)", (idempotency_key, "committed"))
        if self.mode == "response_lost":
            return ActionExecutionResult.unknown_outcome()
        return ActionExecutionResult.succeeded({"effect": "simulated_committed"})

    def counts(self):
        with sqlite3.connect(self.path) as db:
            return {
                "dispatch_count": db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0],
                "effect_count": db.execute("SELECT COUNT(*) FROM effects").fetchone()[0],
            }


@pytest.mark.asyncio
async def test_persistent_side_effect_matrix(db_factory, tmp_path):
    cases = []
    for mode in ("approved", "denied", "unauthorized", "response_lost", "definite_failure"):
        spec = {
            "kind": "builtin",
            "identity": "create_ticket",
            "description": "Simulated write",
            "input_schema": {
                "type": "object",
                "properties": {"customer_ref": {"type": "string"}, "subject": {"type": "string"}},
                "required": ["customer_ref", "subject"],
                "additionalProperties": False,
            },
            "effect": "WRITE",
            "risk_level": "HIGH",
            "approval_policy": "ALWAYS",
        }
        async with db_factory() as db:
            base = await _seed(db, label=f"ledger-{uuid4().hex}", tool_spec=spec)
        context = base["context"].model_copy(
            update={"permissions": base["context"].permissions | {"approve_action"}}
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
                ),
                ModelResponse(content="done", provider="fake", model="fake"),
            ]
        )
        executor = DurableSimulatedWrite(tmp_path / f"{mode}.sqlite", mode)

        def service(gateway=gateway, executor=executor):
            return AgentRunService(
                db_factory,
                model_gateway_factory=lambda db: gateway,
                approval_service=ApprovalService(db_factory),
                action_runtime=ActionRuntime(
                    session_factory=db_factory,
                    registry=ActionRegistry(
                        session_factory=db_factory, overrides={"create_ticket": executor}
                    ),
                ),
                checkpoint_adapter=LangGraphCheckpointAdapter(
                    os.environ["AGENTHUB_TEST_DATABASE_URL"]
                ),
            )

        first = await service().run(
            context, agent_version_id=base["version"].id, input_text="controlled action"
        )
        assert first.status == "WAITING_APPROVAL"
        async with db_factory() as db:
            approval = await db.scalar(select(Approval).where(Approval.run_id == first.run_id))
        if mode == "unauthorized":
            with pytest.raises(AgentHubError) as denied:
                await ApprovalService(db_factory).decide(
                    base["context"], approval.id, decision=ApprovalDecisionStatus.APPROVED
                )
            assert denied.value.status_code == 403
            assert executor.counts() == {"dispatch_count": 0, "effect_count": 0}
        decision = (
            ApprovalDecisionStatus.DENIED
            if mode in {"denied", "unauthorized"}
            else ApprovalDecisionStatus.APPROVED
        )
        await ApprovalService(db_factory).decide(context, approval.id, decision=decision)
        started = perf_counter()
        restarted = service()  # Object reconstruction, explicitly not an OS process restart.
        resumed = await restarted.resume(context, run_id=first.run_id, approval_id=approval.id)
        resume_ms = (perf_counter() - started) * 1000
        await restarted.resume(context, run_id=first.run_id, approval_id=approval.id)
        async with db_factory() as db:
            persisted = await db.get(Approval, approval.id)
        counts = executor.counts()
        expected_effects = 1 if mode in {"approved", "response_lost"} else 0
        assert counts["effect_count"] == expected_effects
        assert counts["dispatch_count"] == (0 if mode in {"denied", "unauthorized"} else 1)
        if mode == "response_lost":
            assert (
                resumed.status == "NEEDS_ATTENTION"
                and persisted.execution_status == "UNKNOWN_OUTCOME"
            )
        elif mode == "definite_failure":
            assert resumed.status != "NEEDS_ATTENTION" and persisted.execution_status == "FAILED"
        cases.append(
            {
                "case": mode,
                "run_id": str(first.run_id),
                "approval_id": str(approval.id),
                "decision_status": persisted.decision_status,
                "execution_status": persisted.execution_status,
                "run_status": resumed.status,
                "resume_ms": resume_ms,
                **counts,
            }
        )
    report = {
        "provider": "controlled",
        "side_effect": "durable_sqlite_simulation",
        "cases": cases,
        "R01": {"duplicate_effect_actions": 0, "tested_actions": 5},
        "R02": {
            "denied_effects": 0,
            "denied_cases": 1,
            "unauthorized_effects": 0,
            "unauthorized_cases": 1,
        },
        "R04": {
            "unknown_correct": 1,
            "unknown_cases": 1,
            "definite_failure_false_unknown": 0,
            "definite_failure_cases": 1,
        },
        "R03": {
            "status": "NOT_AVAILABLE",
            "reason": "no_real_process_crash_or_reconciliation_window_in_this_matrix",
        },
    }
    if output := os.getenv("AGENTHUB_RELIABILITY_OUTPUT"):
        Path(output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
