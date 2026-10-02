"""Ten controlled persistent scenarios; no model-quality claim or provider calls."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from packages.agent_runtime.models import AgentRun
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.business import SupportPostcondition, inspect_support_outcome
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.observations import observation_identity
from packages.evaluation.reproducibility import default_evaluator_manifest
from packages.evaluation.runner import ExperimentRunner
from packages.tools.models import Customer, Ticket
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m7b_experiments import _manager_context
from tests.integration.test_m7c_experiment_runner import (
    _CountingDriver,
    _ready_run,
    db_factory,
    migrated_database,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="Real test PostgreSQL required"
    ),
]

# Import and reuse the established database fixtures instead of adding bootstrap machinery.
__all__ = ["db_factory", "migrated_database"]


@pytest.mark.asyncio
async def test_ten_controlled_business_scenarios_and_report(db_factory):
    cases = [
        ("created", 1, 1, "OPEN", "SUCCEEDED", False, True),
        ("claimed_but_absent", 0, 1, "OPEN", "SUCCEEDED", False, False),
        ("wrong_customer", 1, 1, "OPEN", "SUCCEEDED", True, False),
        ("duplicate_business_effect", 2, 1, "OPEN", "SUCCEEDED", False, False),
        ("wrong_ticket_state", 1, 1, "CLOSED", "SUCCEEDED", False, False),
        ("denied_no_effect", 0, 0, "OPEN", "SUCCEEDED", False, True),
        ("denied_but_effect", 1, 0, "OPEN", "SUCCEEDED", False, False),
        ("unknown_preserved", 0, 0, "OPEN", "NEEDS_ATTENTION", False, True),
        ("wrong_run_state", 1, 1, "OPEN", "FAILED", False, False),
        ("run_missing", 1, 1, "OPEN", None, False, None),
    ]
    records = []
    async with db_factory() as session:
        base = await _seed(session, label="interview-foundation")
        context = _manager_context(base)
        for name, actual, expected, ticket_status, run_status, wrong, success in cases:
            customer = Customer(
                workspace_id=base["workspace_id"], customer_ref=uuid4().hex, name="fixture"
            )
            other = Customer(
                workspace_id=base["workspace_id"], customer_ref=uuid4().hex, name="other"
            )
            session.add_all([customer, other])
            await session.flush()
            keys = (uuid4().hex, uuid4().hex)
            for ordinal in range(actual):
                session.add(
                    Ticket(
                        workspace_id=base["workspace_id"],
                        customer_id=other.id if wrong else customer.id,
                        ticket_ref=uuid4().hex,
                        subject="fixture only",
                        status=ticket_status,
                        idempotency_key=keys[ordinal],
                    )
                )
            run = AgentRun(
                workspace_id=base["workspace_id"],
                agent_version_id=base["version"].id,
                created_by=base["user"].id,
                input_text="controlled fixture",
                status=run_status or "RUNNING",
            )
            session.add(run)
            await session.commit()
            condition = SupportPostcondition(
                base["workspace_id"],
                customer.id,
                keys,
                expected,
                allowed_run_statuses=("NEEDS_ATTENTION",)
                if name == "unknown_preserved"
                else ("SUCCEEDED",),
            )
            observed = await inspect_support_outcome(
                session, context, condition, run_id=run.id if run_status else None
            )
            assert observed["success"] is success, name
            assert observed["status"] == ("NOT_AVAILABLE" if success is None else "AVAILABLE")
            records.append(
                {
                    "case_id": name,
                    **observation_identity(
                        "controlled", {"business_outcome": "workspace_scoped_ticket_query"}
                    ),
                    "business_outcome": observed,
                    "expected_verdict": success,
                    "agent_version_id": str(base["version"].id),
                    "run_id": str(run.id),
                }
            )
        with pytest.raises(AgentHubError) as forbidden:
            await inspect_support_outcome(
                session, context, replace(condition, workspace_id=uuid4()), run_id=run.id
            )
        assert forbidden.value.code == "FORBIDDEN"
        with pytest.raises(AgentHubError):
            await inspect_support_outcome(
                session,
                context.model_copy(update={"permissions": frozenset()}),
                condition,
                run_id=run.id,
            )
    directory = os.environ.get("AGENTHUB_MI1_REPORT_DIR")
    if directory:
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        payload = {
            "report_schema_version": 1,
            "source": "controlled_postgresql_fixtures",
            "real_provider_eval": "NOT_RUN",
            "base_build_sha": os.environ.get("AGENTHUB_BUILD_SHA", "UNKNOWN"),
            "dataset_fixture_hash": canonical_json_hash(cases),
            "snapshot": "NOT_APPLICABLE_controlled_ticket_fixtures",
            "pricing": "NOT_APPLICABLE_no_provider_calls",
            "model": "NOT_APPLICABLE_no_provider_calls",
            "evaluator_manifest": default_evaluator_manifest(),
            "case_count": len(records),
            "source_groups": {"runtime": 0, "controlled": len(records), "synthetic": 0},
            "business_denominator": len(records),
            "unknown_business_count": sum(
                r["business_outcome"]["success"] is None for r in records
            ),
            "records": records,
        }
        (destination / "controlled-business.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        lines = [
            "# M-I1 controlled business checks",
            "",
            "Real PostgreSQL; controlled fixtures; no provider quality claim.",
            "",
            "| Case | Verdict | Failed checks |",
            "| --- | --- | --- |",
        ]
        for record in records:
            outcome = record["business_outcome"]
            failures = ", ".join(outcome["failed_checks"])
            lines.append(f"| {record['case_id']} | {outcome['success']} | {failures} |")
        (destination / "controlled-business.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


@pytest.mark.asyncio
async def test_v1_and_v2_snapshots_stay_frozen_with_shared_service(db_factory, monkeypatch):
    import packages.evaluation.experiments as experiments

    def historical_manifest(*args):
        manifest = default_evaluator_manifest(*args)
        manifest.pop("observation_schema_version")
        manifest["evaluator_versions"].pop("support-postcondition-evaluator")
        for key in manifest["evaluator_versions"]:
            if key.endswith("-evaluator"):
                manifest["evaluator_versions"][key] = "v1"
        return manifest

    async with db_factory() as session:
        base = await _seed(session, label="historical-observations")
        context = _manager_context(base)
        with monkeypatch.context() as patch:
            patch.setattr(experiments, "default_evaluator_manifest", historical_manifest)
            old_run = await _ready_run(session, base, case_count=1)
        new_run = await _ready_run(session, base, case_count=1)
    runner = ExperimentRunner(db_factory, driver=_CountingDriver())
    for run in (old_run, new_run):
        await runner.execute(run_id=run.id, owner="history-worker", settings=Settings(testing=True))
    shared = EvaluationMetricsService()
    async with db_factory() as session:
        old = await shared.materialize_metrics(session, context=context, run_id=old_run.id)
        first_read = await shared.get_persisted_metrics(session, context=context, run_id=old_run.id)
        new = await shared.materialize_metrics(session, context=context, run_id=new_run.id)
        reread = await shared.get_persisted_metrics(session, context=context, run_id=old_run.id)
        assert first_read == reread
        assert old["metrics_hash"] == reread["metrics_hash"]
        assert shared.registry.version_for("RETRIEVAL") == "v1"
        for metrics in old["variants"].values():
            assert metrics["task_success"]["evaluator_version"] == "v1"
            assert "case_cost_coverage" not in metrics
        for metrics in new["variants"].values():
            assert metrics["task_success"]["evaluator_version"] == "v2"
            assert metrics["case_cost_coverage"]["evaluator_version"] == "v2"
