"""Verify one persisted AE-Real-1 run and write a complete evidence record."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from packages.agent_runtime.models import RunStep
from packages.approvals.models import Approval
from packages.control_plane.services import TenantService
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.evaluation.metrics import EvaluatorRegistry
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.models import EvaluationExperimentRun
from packages.evaluation.release_gate import EvaluationReleaseGateService
from packages.evaluation.statistics_service import EvaluationStatisticsService
from packages.tools.models import Ticket

URL = "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub_ae_real"
RUN = UUID("a8e9e884-3e3e-497e-9f2a-86fbd0d6ab13")
OUTPUT = Path("docs/milestones/ae-real-1-evidence.json")


def _gate_checks(statistics: dict) -> list[dict]:
    """Run the statistics-backed gate rules.

    The real branch uses the persisted run's statistics.  The controlled branch uses
    synthetic values ONLY to exercise the contract (small sample has no failures), and
    is labelled as such.
    """

    service = EvaluationReleaseGateService()
    variant_id = next(iter(statistics["variants"]))
    comparison = SimpleNamespace(
        status="COMPLETE",
        metrics={},
        baseline_variant_id=variant_id,
        candidate_variant_id=variant_id,
    )
    checks = []

    def run(rule: str, threshold: float, payload: dict, basis: str) -> None:
        policy = service._validate_policy(
            {"rules": [{"metric": "pass_hat_k", "rule": rule, "threshold": threshold}]}
        )
        status, results, _ = service._evaluate(comparison, policy, None, payload)
        checks.append(
            {
                "rule": rule,
                "basis": basis,
                "status": status,
                "result": results[0] if results else None,
            }
        )

    run("MIN_PASS_HAT_K", 1.0, statistics, "real_persisted_run")
    controlled = {
        **statistics,
        "variants": {
            variant_id: {**statistics["variants"][variant_id], "pass_hat_k": 0.0}
        },
    }
    run("MIN_PASS_HAT_K", 0.8, controlled, "controlled_contract_only")
    incomplete = {
        **statistics,
        "variants": {
            variant_id: {**statistics["variants"][variant_id], "complete_case_rate": 0.2}
        },
    }
    run("MIN_PASS_HAT_K", 0.8, incomplete, "controlled_contract_only")

    paired_policy = service._validate_policy(
        {
            "rules": [
                {
                    "metric": "success_rate_delta",
                    "rule": "MIN_CI_LOWER_BOUND",
                    "threshold": 0.0,
                }
            ]
        }
    )
    paired = {
        "paired": {
            "delta": 0.01,
            "interval": {"lower": 0.005, "upper": 0.02},
            "pair_count": 3,
        }
    }
    status, results, _ = service._evaluate(comparison, paired_policy, None, paired)
    checks.append(
        {
            "rule": "MIN_CI_LOWER_BOUND",
            "basis": "controlled_contract_only",
            "status": status,
            "result": results[0] if results else None,
        }
    )
    return checks


async def main() -> int:
    engine, factory = create_database(URL)
    try:
        async with factory() as session:
            run = await session.get(EvaluationExperimentRun, RUN)
            if run is None:
                raise SystemExit("run not found")
            principal = PrincipalContext(
                request_id="ae-real-1-verify",
                trace_id="ae-real-1-verify",
                user_id=str(run.created_by),
            )
            context = (
                await TenantService().get_workspace_access(
                    session, principal=principal, workspace_id=run.workspace_id
                )
            ).context
            context = context.model_copy(
                update={
                    "permissions": context.permissions
                    | {"evaluation_read", "evaluation_manage", "workspace_read"}
                }
            )
            metrics_service = EvaluationMetricsService(EvaluatorRegistry())
            experiment = await metrics_service._load_experiment(
                session, run.workspace_id, run.experiment_id
            )
            registry = EvaluatorRegistry().for_manifest(experiment.evaluator_manifest)
            rows = await metrics_service._load_and_validate_case_set(
                session, run.workspace_id, run, experiment
            )

            case_records = []
            run_ids = []
            for case, item, _variant in rows:
                run_ids.append(case.agent_run_id)
                evaluated = registry.evaluate(item.category, item.expected, case.observation)
                metrics = {
                    name: {
                        "status": metric.status.value,
                        "value": float(metric.value) if metric.value is not None else None,
                        "reason": metric.reason,
                    }
                    for name, metric in sorted(evaluated.items())
                }
                observation = case.observation or {}
                case_records.append(
                    {
                        "case_key": item.case_key,
                        "category": item.category,
                        "expected": dict(item.expected),
                        "agent_run_id": str(case.agent_run_id),
                        "status": case.status,
                        "observed_agent_status": case.observed_agent_status,
                        "total_tokens": case.total_tokens,
                        "cost_amount": (
                            str(case.cost_amount) if case.cost_amount is not None else None
                        ),
                        "cost_currency": case.cost_currency,
                        "cost_status": observation.get("cost_status"),
                        "terminal_status": observation.get("terminal_status"),
                        "proposed_tool_sequence": observation.get("proposed_tool_sequence"),
                        "executed_tool_sequence": observation.get("executed_tool_sequence"),
                        "proposed_calls": observation.get("proposed_calls"),
                        "executed_calls": observation.get("executed_calls"),
                        "approval_required": observation.get("approval_required"),
                        "approval_decision": observation.get("approval_decision"),
                        "action_executed": observation.get("action_executed"),
                        "approval_facts": observation.get("approval_facts"),
                        "failure_taxonomy": observation.get("failure_taxonomy"),
                        "metrics": metrics,
                    }
                )

            case_by_run = {record["agent_run_id"]: record for record in case_records}
            steps = list(
                await session.scalars(
                    select(RunStep)
                    .where(
                        RunStep.workspace_id == run.workspace_id,
                        RunStep.agent_run_id.in_(run_ids),
                    )
                    .order_by(RunStep.agent_run_id, RunStep.sequence_number)
                )
            )
            for step in steps:
                record = case_by_run.get(str(step.agent_run_id))
                if record is None:
                    continue
                record.setdefault("runtime_steps", []).append(
                    {
                        "sequence_number": step.sequence_number,
                        "kind": step.kind,
                        "status": step.status,
                        "tool_identities": (step.safe_metadata or {}).get("tool_identities") or [],
                        "tool_results": (step.safe_metadata or {}).get("tool_results") or [],
                        "logical_action_ids": (step.safe_metadata or {}).get("logical_action_ids")
                        or [],
                    }
                )

            approvals = list(
                await session.scalars(
                    select(Approval)
                    .where(
                        Approval.workspace_id == run.workspace_id,
                        Approval.run_id.in_(run_ids),
                    )
                    .order_by(Approval.created_at)
                )
            )
            for approval in approvals:
                record = case_by_run.get(str(approval.run_id))
                if record is None:
                    continue
                record.setdefault("approval_rows", []).append(
                    {
                        "approval_id": str(approval.id),
                        "tool_identity": approval.tool_identity,
                        "canonical_args_hash": approval.canonical_args_hash,
                        "decision_status": approval.decision_status,
                        "execution_status": approval.execution_status,
                        "logical_action_id": approval.logical_action_id,
                        "execution_attempt_count": approval.execution_attempt_count,
                    }
                )

            tickets = list(
                await session.scalars(
                    select(Ticket).where(Ticket.workspace_id == run.workspace_id)
                )
            )
            statistics = await EvaluationStatisticsService().compute(
                session, context=context, run_id=RUN
            )
            variant = rows[0][2]
            variant_metadata = dict(variant.variant_metadata or {})
            metrics_service_for_run = EvaluationMetricsService()
            await metrics_service_for_run.materialize_metrics(
                session, context=context, run_id=RUN
            )
            persisted_metrics = await metrics_service_for_run.get_persisted_metrics(
                session, context=context, run_id=RUN
            )
            gate_checks = _gate_checks(statistics)
            evidence = {
                "status": "COMPLETED",
                "run_id": str(RUN),
                "experiment_run_id": str(RUN),
                "experiment_id": str(run.experiment_id),
                "workspace_id": str(run.workspace_id),
                "run_status": run.status,
                "repetitions": run.repetitions,
                "split": run.split,
                "purpose": run.purpose,
                "evidence_level": run.evidence_level,
                "variant_id": str(variant.id),
                "variant_hash": variant.variant_hash,
                "pricing_snapshot_id": str(variant.pricing_snapshot_id),
                "variant_metadata": variant_metadata,
                "case_results": case_records,
                "business_outcome": {
                    "approved_ticket_count": sum(
                        1 for t in tickets if t.subject.startswith("AE-REAL-APPROVED-")
                    ),
                    "denied_ticket_count": sum(
                        1 for t in tickets if t.subject.startswith("AE-REAL-DENIED-")
                    ),
                    "all_tickets": [
                        {"ticket_ref": t.ticket_ref, "subject": t.subject, "status": t.status}
                        for t in tickets
                    ],
                },
                "statistics": statistics,
                "persisted_metrics": persisted_metrics,
                "gate_checks": gate_checks,
            }
            OUTPUT.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
