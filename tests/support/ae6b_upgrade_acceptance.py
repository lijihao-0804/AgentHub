"""Prepare a fresh PostgreSQL 0050 -> 0051 compatibility rehearsal.

The default mode requires an empty, isolated database. It creates ordinary
non-interactive M7 data at 0050, runs the real ExperimentRunner with a
controlled driver, materializes metrics, upgrades to 0051, and verifies the
same safe hashes and metric integrity.

``--history-only`` independently checks the two checked-in AE-6b evidence JSON
files against the baseline without opening a database.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.build_identity import StaticBuildIdentityProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationDatasetVersion,
    EvaluationExperiment,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    EvaluationExperimentVariant,
)
from packages.evaluation.runner import CaseExecutionObservation, ExperimentRunner
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m7b_experiments import (
    _item,
    _manager_context,
    _pricing,
    _published_version,
)
from tests.support.ae6b_database_urls import (
    async_test_database_url,
    require_isolated_test_database_url,
    sync_test_database_url,
)

MIGRATION_BASE = "0050_interactive_category"
MIGRATION_HEAD = "0051_interactive_lifecycle"
HISTORICAL_EVIDENCE = {
    "ae-6b-evidence.json": {
        "sha256": "3fd8d810d8301537dc3622e845887c2b9e50812613b4ecd2d7fd824091017d8e",
        "experiment_run_id": "6577b772-b36d-4d61-a889-dbfd9347cd85",
    },
    "ae-6b-evidence-redacted.json": {
        "sha256": "4428469ce5d654da4d7deac81b8d0b7a9efaa7ff129f68336f2f9fec836ccf9c",
        "experiment_run_id": "1914c232-bd3e-40fa-9a99-0bfb2d4f4ecc",
    },
}
EVIDENCE_DIR = ROOT / "docs" / "milestones"
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


class CompatibilityError(RuntimeError):
    """A bounded failure whose code is safe to place in the receipt."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CompatibilityState:
    context: WorkspaceExecutionContext
    workspace_id: UUID
    dataset_id: UUID
    dataset_version_id: UUID
    dataset_item_id: UUID
    experiment_id: UUID
    variant_id: UUID
    run_id: UUID
    case_id: UUID
    before_hashes: dict[str, Any]

    @property
    def identity(self) -> dict[str, str]:
        return {
            "workspace_id": str(self.workspace_id),
            "dataset_id": str(self.dataset_id),
            "dataset_version_id": str(self.dataset_version_id),
            "dataset_item_id": str(self.dataset_item_id),
            "experiment_id": str(self.experiment_id),
            "variant_id": str(self.variant_id),
            "experiment_run_id": str(self.run_id),
            "case_result_id": str(self.case_id),
        }


class _ControlledRetrievalDriver:
    """One deterministic non-interactive case; no model gateway is involved."""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        session: AsyncSession | None,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> CaseExecutionObservation:
        del session, run
        self.calls += 1
        relevant = list(item.expected.get("relevant_chunk_ids", []))
        return CaseExecutionObservation(
            observation={
                "category": item.category,
                "variant_hash": variant.variant_hash,
                "candidate_chunk_ids": relevant,
                "final_chunk_ids": relevant,
                "candidate_top_k": 20,
                "final_top_k": 5,
                "citation_ids": relevant,
                "driver_kind": "controlled",
                "observation_schema_version": 2,
            },
            latency_ms=12,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cached_tokens=0,
        )


def _json_projection(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_projection(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_projection(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise CompatibilityError(f"T15_UNSUPPORTED_HASH_VALUE:{type(value).__name__}")


def _safe_hash(value: Any) -> str:
    return canonical_json_hash(_json_projection(value))


def _historical_evidence_receipt() -> dict[str, Any]:
    expected_entries = HISTORICAL_EVIDENCE
    if not isinstance(expected_entries, dict) or len(expected_entries) != 2:
        raise CompatibilityError("T15_HISTORICAL_BASELINE_INVALID")

    checked: dict[str, Any] = {}
    for filename, expected in sorted(expected_entries.items()):
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise CompatibilityError("T15_HISTORICAL_FILENAME_INVALID")
        if not isinstance(expected, dict):
            raise CompatibilityError("T15_HISTORICAL_BASELINE_INVALID")
        evidence_path = EVIDENCE_DIR / filename
        raw = evidence_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected.get("sha256"):
            raise CompatibilityError(f"T15_HISTORICAL_HASH_MISMATCH:{filename}")
        payload = json.loads(raw)
        if payload.get("experiment_run_id") != expected.get("experiment_run_id"):
            raise CompatibilityError(f"T15_HISTORICAL_RUN_MISMATCH:{filename}")
        checked[filename] = {
            "sha256": digest,
            "experiment_run_id": expected["experiment_run_id"],
        }
    return checked


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    head = completed.stdout.strip()
    if not _SHA_PATTERN.fullmatch(head):
        raise CompatibilityError("T15_GIT_HEAD_INVALID")
    return head


def _require_empty_database(database_url: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=False)
    try:
        with engine.connect() as connection:
            extra_schema = connection.scalar(
                text(
                    """
                    SELECT n.nspname
                    FROM pg_namespace AS n
                    WHERE n.nspname <> 'public'
                      AND n.nspname NOT IN ('information_schema')
                      AND n.nspname NOT LIKE 'pg_%'
                    LIMIT 1
                    """
                )
            )
            if extra_schema is not None:
                raise CompatibilityError("T15_DATABASE_HAS_EXISTING_SCHEMA")
            relation = connection.scalar(
                text(
                    """
                    SELECT c.relname
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                      AND n.nspname NOT LIKE 'pg_toast%'
                      AND n.nspname NOT LIKE 'pg_temp%'
                      AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
                    LIMIT 1
                    """
                )
            )
            if relation is not None:
                raise CompatibilityError("T15_DATABASE_NOT_EMPTY")
    finally:
        engine.dispose()


def _alembic_config(async_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(ROOT))
    os.environ["AGENTHUB_DATABASE_URL"] = async_url
    get_settings.cache_clear()
    return config


def _upgrade(async_url: str, target: str) -> None:
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    try:
        command.upgrade(_alembic_config(async_url), target)
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


def _current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url, pool_pre_ping=False)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _interactive_tables_present(database_url: str) -> bool:
    engine = create_engine(database_url, pool_pre_ping=False)
    try:
        with engine.connect() as connection:
            return bool(
                connection.scalar(
                    text(
                        """
                        SELECT to_regclass(
                          'public.evaluation_interactive_episodes'
                        ) IS NOT NULL
                        AND to_regclass(
                          'public.evaluation_interactive_episode_turns'
                        ) IS NOT NULL
                        """
                    )
                )
            )
    finally:
        engine.dispose()


def _safe_hashes(
    *,
    dataset_version: EvaluationDatasetVersion,
    item: EvaluationDatasetItem,
    experiment: EvaluationExperiment,
    variant: EvaluationExperimentVariant,
    case: EvaluationExperimentCaseResult,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    dataset_version_projection = {
        "dataset_id": dataset_version.dataset_id,
        "version_number": dataset_version.version_number,
        "schema_version": dataset_version.schema_version,
        "content_hash": dataset_version.content_hash,
        "status": dataset_version.status,
    }
    item_projection = {
        "id": item.id,
        "dataset_version_id": item.dataset_version_id,
        "case_key": item.case_key,
        "split": item.split,
        "category": item.category,
        "input": item.input,
        "expected": item.expected,
        "tags": item.tags,
        "source_provenance": item.source_provenance,
        "ordinal": item.ordinal,
    }
    experiment_projection = {
        "id": experiment.id,
        "dataset_version_id": experiment.dataset_version_id,
        "dataset_content_hash": experiment.dataset_content_hash,
        "dataset_schema_version": experiment.dataset_schema_version,
        "split": experiment.split,
        "purpose": experiment.purpose,
        "repetitions": experiment.repetitions,
        "evidence_level": experiment.evidence_level,
        "build_sha": experiment.build_sha,
        "evaluator_manifest": experiment.evaluator_manifest,
        "spec_hash": experiment.spec_hash,
        "status": experiment.status,
    }
    variant_projection = {
        "id": variant.id,
        "experiment_id": variant.experiment_id,
        "label": variant.label,
        "agent_version_id": variant.agent_version_id,
        "resolved_spec_hash": variant.resolved_spec_hash,
        "pricing_snapshot_id": variant.pricing_snapshot_id,
        "pricing_snapshot_hash": variant.pricing_snapshot_hash,
        "effective_knowledge_snapshots": variant.effective_knowledge_snapshots,
        "effective_wiki_publications": variant.effective_wiki_publications,
        "variant_metadata": variant.variant_metadata,
        "variant_hash": variant.variant_hash,
        "ordinal": variant.ordinal,
    }
    case_projection = {
        "id": case.id,
        "experiment_run_id": case.experiment_run_id,
        "experiment_variant_id": case.experiment_variant_id,
        "dataset_item_id": case.dataset_item_id,
        "repetition_index": case.repetition_index,
        "case_execution_key": case.case_execution_key,
        "status": case.status,
        "agent_run_id": case.agent_run_id,
        "latency_ms": case.latency_ms,
        "input_tokens": case.input_tokens,
        "output_tokens": case.output_tokens,
        "total_tokens": case.total_tokens,
        "cached_tokens": case.cached_tokens,
        "cost_amount": case.cost_amount,
        "cost_currency": case.cost_currency,
        "failure_code": case.failure_code,
        "observed_agent_status": case.observed_agent_status,
        "observed_agent_failure_code": case.observed_agent_failure_code,
        "safe_failure_message": case.safe_failure_message,
        "observation": case.observation,
    }
    return {
        "dataset": {
            "version_hash": _safe_hash(dataset_version_projection),
            "item_hash": _safe_hash(item_projection),
        },
        "experiment": {
            "spec_hash": experiment.spec_hash,
            "projection_hash": _safe_hash(experiment_projection),
            "variant_hash": variant.variant_hash,
            "variant_projection_hash": _safe_hash(variant_projection),
        },
        "case": {
            "projection_hash": _safe_hash(case_projection),
            "observation_hash": _safe_hash(case.observation),
        },
        "metrics": {
            "evaluator_manifest_hash": metrics["evaluator_manifest_hash"],
            "case_result_set_hash": metrics["case_result_set_hash"],
            "metrics_hash": metrics["metrics_hash"],
            "payload_hash": _safe_hash(metrics),
        },
    }


async def _seed_noninteractive_at_0050(
    async_url: str,
    *,
    build_sha: str,
) -> CompatibilityState:
    engine, factory = create_database(async_url)
    try:
        async with factory() as session:
            base = await _seed(session, label=f"ae6b-t15-{os.urandom(8).hex()}")
            context = _manager_context(base)
            dataset, dataset_version = await _published_version(
                session,
                base,
                items=[
                    _item(
                        "t15-compatibility-retrieval",
                        split="DEV",
                        ordinal=0,
                        category="RETRIEVAL",
                    )
                ],
            )
            pricing = await _pricing(session, base)
            service = ExperimentService(StaticBuildIdentityProvider(build_sha))
            experiment = await service.create_experiment(
                session,
                context=context,
                name="AE6B T15 0050 compatibility",
                description=None,
                dataset_version_id=dataset_version.id,
                split="DEV",
                purpose="DEVELOPMENT",
                repetitions=1,
            )
            variant = await service.add_variant(
                session,
                context=context,
                experiment_id=experiment.id,
                label="controlled-retrieval",
                agent_version_id=base["version"].id,
                pricing_snapshot_id=pricing.id,
                ordinal=0,
            )
            experiment = await service.finalize_experiment(
                session,
                context=context,
                experiment_id=experiment.id,
            )
            run, _ = await service.create_run(
                session,
                context=context,
                experiment_id=experiment.id,
            )

        driver = _ControlledRetrievalDriver()
        runner = ExperimentRunner(factory, driver=driver)
        await runner.execute(
            run_id=run.id,
            owner=f"ae6b-t15-{os.urandom(8).hex()}",
            settings=Settings(
                _env_file=None,
                testing=True,
                database_url=async_url,
                evaluation_runner_lease_seconds=300,
                evaluation_runner_heartbeat_seconds=5,
            ),
        )
        if driver.calls != 1:
            raise CompatibilityError("T15_CONTROLLED_DRIVER_CALL_COUNT_MISMATCH")

        metrics_service = EvaluationMetricsService()
        async with factory() as session:
            await metrics_service.materialize_metrics(
                session,
                context=context,
                run_id=run.id,
            )

        async with factory() as session:
            stored_dataset_version = await session.get(EvaluationDatasetVersion, dataset_version.id)
            stored_item = await session.scalar(
                select(EvaluationDatasetItem).where(
                    EvaluationDatasetItem.workspace_id == base["workspace_id"],
                    EvaluationDatasetItem.dataset_version_id == dataset_version.id,
                )
            )
            stored_experiment = await session.get(EvaluationExperiment, experiment.id)
            stored_variant = await session.get(EvaluationExperimentVariant, variant.id)
            stored_run = await session.get(EvaluationExperimentRun, run.id)
            stored_case = await session.scalar(
                select(EvaluationExperimentCaseResult).where(
                    EvaluationExperimentCaseResult.workspace_id == base["workspace_id"],
                    EvaluationExperimentCaseResult.experiment_run_id == run.id,
                )
            )
            if (
                stored_dataset_version is None
                or stored_item is None
                or stored_experiment is None
                or stored_variant is None
                or stored_run is None
                or stored_case is None
            ):
                raise CompatibilityError("T15_PERSISTED_DATA_MISSING")
            if stored_run.status != "SUCCEEDED" or stored_case.status != "SUCCEEDED":
                raise CompatibilityError("T15_CONTROLLED_RUN_NOT_SUCCESSFUL")
            metrics = await metrics_service.get_persisted_metrics(
                session,
                context=context,
                run_id=run.id,
            )
            before_hashes = _safe_hashes(
                dataset_version=stored_dataset_version,
                item=stored_item,
                experiment=stored_experiment,
                variant=stored_variant,
                case=stored_case,
                metrics=metrics,
            )
            return CompatibilityState(
                context=context,
                workspace_id=base["workspace_id"],
                dataset_id=dataset.id,
                dataset_version_id=dataset_version.id,
                dataset_item_id=stored_item.id,
                experiment_id=experiment.id,
                variant_id=variant.id,
                run_id=run.id,
                case_id=stored_case.id,
                before_hashes=before_hashes,
            )
    finally:
        await engine.dispose()


async def _read_back_at_0051(
    async_url: str,
    *,
    state: CompatibilityState,
) -> dict[str, Any]:
    engine, factory = create_database(async_url)
    try:
        metrics_service = EvaluationMetricsService()
        async with factory() as session:
            dataset_version = await session.get(EvaluationDatasetVersion, state.dataset_version_id)
            item = await session.get(EvaluationDatasetItem, state.dataset_item_id)
            experiment = await session.get(EvaluationExperiment, state.experiment_id)
            variant = await session.get(EvaluationExperimentVariant, state.variant_id)
            run = await session.get(EvaluationExperimentRun, state.run_id)
            case = await session.get(EvaluationExperimentCaseResult, state.case_id)
            if (
                dataset_version is None
                or item is None
                or experiment is None
                or variant is None
                or run is None
                or case is None
            ):
                raise CompatibilityError("T15_READBACK_DATA_MISSING")
            if (
                dataset_version.status != "PUBLISHED"
                or run.status != "SUCCEEDED"
                or case.status != "SUCCEEDED"
            ):
                raise CompatibilityError("T15_READBACK_STATE_CHANGED")
            metrics = await metrics_service.get_persisted_metrics(
                session,
                context=state.context,
                run_id=state.run_id,
            )
            after_hashes = _safe_hashes(
                dataset_version=dataset_version,
                item=item,
                experiment=experiment,
                variant=variant,
                case=case,
                metrics=metrics,
            )
        if after_hashes != state.before_hashes:
            raise CompatibilityError("T15_HASH_DRIFT_AFTER_0051")
        return after_hashes
    finally:
        await engine.dispose()


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            receipt,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _safe_failure_code(error: BaseException) -> str | None:
    code = getattr(error, "code", None)
    if isinstance(error, AgentHubError) and isinstance(code, str):
        return code
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the bounded AE-6b 0050 -> 0051 compatibility rehearsal.",
    )
    parser.add_argument(
        "--database-url",
        help="Explicit fresh isolated PostgreSQL URL; never written to the receipt.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path for the safe JSON receipt.",
    )
    parser.add_argument(
        "--history-only",
        action="store_true",
        help="Only verify the two checked-in evidence JSON SHA-256 values.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.history_only and args.database_url:
        parser.error("--history-only cannot be combined with --database-url")
    if not args.history_only and not args.database_url:
        parser.error("--database-url is required outside --history-only mode")

    receipt_path: Path = args.output.expanduser()
    phase = "history"
    receipt: dict[str, Any] = {
        "schema_version": "ae6b-t15-compatibility-v1",
        "mode": "history_only" if args.history_only else "migration_compatibility",
        "status": "FAILED",
    }
    try:
        build_sha = _git_head()
        historical = _historical_evidence_receipt()
        receipt.update({"head": build_sha, "historical_evidence": historical})
        if args.history_only:
            receipt["status"] = "PASS"
            _write_receipt(receipt_path, receipt)
            return 0

        phase = "database_guard"
        guarded_url = require_isolated_test_database_url(args.database_url)
        sync_url = sync_test_database_url(
            guarded_url.render_as_string(hide_password=False)
        ).render_as_string(hide_password=False)
        async_url = async_test_database_url(
            guarded_url.render_as_string(hide_password=False)
        ).render_as_string(hide_password=False)
        _require_empty_database(sync_url)

        phase = "upgrade_0050"
        _upgrade(async_url, MIGRATION_BASE)
        if _current_revision(sync_url) != MIGRATION_BASE:
            raise CompatibilityError("T15_BASE_REVISION_MISMATCH")
        if _interactive_tables_present(sync_url):
            raise CompatibilityError("T15_INTERACTIVE_TABLES_PRESENT_AT_0050")

        phase = "controlled_run_at_0050"
        state = asyncio.run(
            _seed_noninteractive_at_0050(
                async_url,
                build_sha=build_sha,
            )
        )

        phase = "upgrade_0051"
        _upgrade(async_url, "head")
        if _current_revision(sync_url) != MIGRATION_HEAD:
            raise CompatibilityError("T15_HEAD_REVISION_MISMATCH")
        if not _interactive_tables_present(sync_url):
            raise CompatibilityError("T15_INTERACTIVE_TABLES_MISSING_AFTER_HEAD")

        phase = "read_back_at_0051"
        after_hashes = asyncio.run(
            _read_back_at_0051(
                async_url,
                state=state,
            )
        )
        receipt.update(
            {
                "status": "PASS",
                "migration": {
                    "base_revision": MIGRATION_BASE,
                    "head_revision": MIGRATION_HEAD,
                    "database_scope": "fresh_isolated_no_drop",
                    "legacy_data_created_before_head": True,
                },
                "identity": state.identity,
                "hashes_before_head": state.before_hashes,
                "hashes_after_head": after_hashes,
                "checks": {
                    "real_experiment_runner": True,
                    "controlled_driver_calls": 1,
                    "provider_calls": 0,
                    "metrics_materialized_before_upgrade": True,
                    "metrics_integrity_verified_after_upgrade": True,
                    "same_safe_hashes_after_upgrade": True,
                    "interactive_tables_created_by_0051": True,
                    "evidence_kind": "DETERMINISTIC_CONFORMANCE",
                },
            }
        )
        _write_receipt(receipt_path, receipt)
        return 0
    except Exception as error:
        failure = {
            **receipt,
            "status": "FAILED",
            "stage": phase,
            "error_type": type(error).__name__,
        }
        safe_code = _safe_failure_code(error)
        if safe_code is not None:
            failure["error_code"] = safe_code
        if isinstance(error, CompatibilityError):
            failure["error_code"] = error.code
        try:
            _write_receipt(receipt_path, failure)
        except Exception:
            pass
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "stage": phase,
                    "error_type": type(error).__name__,
                    "error_code": failure.get("error_code"),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
