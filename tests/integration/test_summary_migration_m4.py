"""PostgreSQL acceptance tests for the durable M4 summary persistence model."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from packages.agent_runtime.summary_models import (
    ThreadSummaryJobSource,
)
from packages.agent_runtime.summary_state import SummaryJobStatus
from packages.core.config.settings import get_settings
from packages.core.database import Base

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
EXPECTED_DATABASE_URL = (
    "postgresql+psycopg://"
    "agenthub:m2_local_test@127.0.0.1:15432/agenthub_context_policy_m2"
)
SUMMARY_TABLES = {
    "thread_summary_artifacts",
    "thread_summary_dispatches",
    "thread_summary_eligibility_records",
    "thread_summary_job_sources",
    "thread_summary_jobs",
    "thread_summary_responses",
}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set the dedicated AGENTHUB_TEST_DATABASE_URL to run M4 migration tests.",
    ),
]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    assert TEST_DATABASE_URL == EXPECTED_DATABASE_URL, (
        "M4 migration tests may only use the explicitly assigned isolated PostgreSQL database."
    )
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    database_engine = create_engine(TEST_DATABASE_URL)
    try:
        yield database_engine
    finally:
        database_engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    # Individual tests use savepoints around each expected database error. This
    # keeps all test evidence visible during a failed assertion.
    with engine.connect() as database_connection:
        transaction = database_connection.begin()
        try:
            yield database_connection
        finally:
            transaction.rollback()


def _assert_deferred_failure_commits(
    connection: Connection,
    statement: str,
    parameters: dict[str, object],
) -> None:
    connection.execute(text("SAVEPOINT before_non_atomic_dispatch"))
    connection.execute(text(statement), parameters)
    connection.execute(text("RELEASE SAVEPOINT before_non_atomic_dispatch"))
    with pytest.raises(DBAPIError):
        connection.execute(text("COMMIT"))
    connection.execute(text("ROLLBACK"))
    connection.execute(text("SAVEPOINT after_non_atomic_dispatch"))


def test_models_cover_the_persisted_contract() -> None:
    assert set(Base.metadata.tables) >= SUMMARY_TABLES
    assert {
        table.name
        for table in Base.metadata.tables.values()
        if table.name.startswith("thread_summary_")
    } == SUMMARY_TABLES
    assert {status.value for status in SummaryJobStatus} == {
        "QUEUED",
        "CLAIMED",
        "DISPATCH_INTENT",
        "RESPONSE_STORED",
        "VALIDATED",
        "PUBLISHED",
        "FAILED",
        "STALE",
        "UNKNOWN_OUTCOME",
    }
    source_columns = set(ThreadSummaryJobSource.__table__.columns.keys())
    assert not {
        "user_input",
        "input_text",
        "final_output",
        "raw_text",
        "content",
    } & source_columns
    assert {
        "turn_id",
        "run_id",
        "source_identity_hash",
        "revision_sha256",
        "user_input_sha256",
        "final_output_sha256",
    } <= source_columns


def _expect_db_error(
    connection: Connection,
    statement: str,
    parameters: dict[str, object] | None = None,
    *,
    kind: type[DBAPIError] = DBAPIError,
) -> DBAPIError:
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(kind) as raised:
            connection.execute(text(statement), parameters or {})
    finally:
        savepoint.rollback()
    return raised.value


def _seed_tenant(
    connection: Connection,
    *,
    label: str | None = None,
) -> dict[str, UUID | str]:
    label = label or uuid4().hex
    user_id = uuid4()
    organization_id = uuid4()
    workspace_id = uuid4()
    credential_id = uuid4()
    profile_id = uuid4()
    agent_id = uuid4()
    version_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    turn_id = uuid4()
    pricing_id = uuid4()
    email = f"summary-m4-{label}@example.test"

    connection.execute(
        text(
            """
            INSERT INTO users (id, email, normalized_email, password_hash)
            VALUES (:id, :email, :email, 'not-used')
            """
        ),
        {"id": user_id, "email": email},
    )
    connection.execute(
        text(
            """
            INSERT INTO organizations (id, name, created_by)
            VALUES (:id, :name, :created_by)
            """
        ),
        {"id": organization_id, "name": f"Summary M4 {label}", "created_by": user_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO organization_memberships (organization_id, user_id, role)
            VALUES (:organization_id, :user_id, 'OWNER')
            """
        ),
        {"organization_id": organization_id, "user_id": user_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO workspaces (id, organization_id, name)
            VALUES (:id, :organization_id, :name)
            """
        ),
        {
            "id": workspace_id,
            "organization_id": organization_id,
            "name": f"Summary M4 {label}",
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO workspace_memberships (workspace_id, user_id, role)
            VALUES (:workspace_id, :user_id, 'DEVELOPER')
            """
        ),
        {"workspace_id": workspace_id, "user_id": user_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO provider_credentials (id, workspace_id, provider, name, secret)
            VALUES (:id, :workspace_id, 'offline', :name, 'offline-test-secret')
            """
        ),
        {
            "id": credential_id,
            "workspace_id": workspace_id,
            "name": f"summary-m4-credential-{label}",
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO model_profiles (
              id, workspace_id, provider_credential_id, model, temperature,
              max_tokens, timeout_seconds, capabilities
            )
            VALUES (
              :id, :workspace_id, :credential_id, 'offline-summary-model',
              0, 512, 5, '{"streaming": false}'::jsonb
            )
            """
        ),
        {
            "id": profile_id,
            "workspace_id": workspace_id,
            "credential_id": credential_id,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO agents (
              id, workspace_id, name, system_prompt, prompt_version,
              model_profile_id, knowledge_binding_mode,
              model_retry_policy, retrieval_config, runtime_config
            )
            VALUES (
              :id, :workspace_id, :name, 'Offline summary persistence test.',
              1, :profile_id, 'PINNED', '{"max_attempts": 1}'::jsonb,
              '{"knowledge_binding_mode": "PINNED", "knowledge_snapshots": []}'::jsonb,
              '{}'::jsonb
            )
            """
        ),
        {
            "id": agent_id,
            "workspace_id": workspace_id,
            "name": f"summary-m4-agent-{label}",
            "profile_id": profile_id,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO agent_versions (
              id, workspace_id, agent_id, version_number, spec_schema_version,
              resolved_spec, resolved_spec_hash, created_by
            )
            VALUES (
              :id, :workspace_id, :agent_id, 1, 1,
              '{"spec_schema_version": 1, "model": {"model": "offline-summary-model"}}'::jsonb,
              :hash, :created_by
            )
            """
        ),
        {
            "id": version_id,
            "workspace_id": workspace_id,
            "agent_id": agent_id,
            "hash": _hash(f"version-{label}"),
            "created_by": user_id,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO agent_threads (
              id, workspace_id, agent_id, title, kind, created_by
            )
            VALUES (:id, :workspace_id, :agent_id, :title, 'general', :created_by)
            """
        ),
        {
            "id": thread_id,
            "workspace_id": workspace_id,
            "agent_id": agent_id,
            "title": f"Summary M4 {label}",
            "created_by": user_id,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO agent_runs (
              id, workspace_id, agent_version_id, thread_id,
              input_text, final_output, status, created_by
            )
            VALUES (
              :id, :workspace_id, :version_id, :thread_id,
              :input_text, :final_output, 'SUCCEEDED', :created_by
            )
            """
        ),
        {
            "id": run_id,
            "workspace_id": workspace_id,
            "version_id": version_id,
            "thread_id": thread_id,
            "input_text": f"raw source question {label}",
            "final_output": f"raw source answer {label}",
            "created_by": user_id,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO thread_turns (
              id, workspace_id, thread_id, sequence, user_input,
              agent_run_id, client_token, input_hash
            )
            VALUES (
              :id, :workspace_id, :thread_id, 1, :input_text,
              :run_id, :client_token, :input_hash
            )
            """
        ),
        {
            "id": turn_id,
            "workspace_id": workspace_id,
            "thread_id": thread_id,
            "input_text": f"raw source question {label}",
            "run_id": run_id,
            "client_token": f"summary-m4-{label}",
            "input_hash": _hash(f"source-question-{label}"),
        },
    )
    connection.execute(
        text("UPDATE agent_runs SET thread_turn_id = :turn_id WHERE id = :run_id"),
        {"turn_id": turn_id, "run_id": run_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO pricing_snapshots (
              id, workspace_id, name, provider, model, currency,
              input_price_per_1m, output_price_per_1m, effective_at,
              source_note, content_hash, created_by
            )
            VALUES (
              :id, :workspace_id, :name, 'offline', 'offline-summary-model', 'CNY',
              1.25, 2.50, :effective_at, 'local test price', :hash, :created_by
            )
            """
        ),
        {
            "id": pricing_id,
            "workspace_id": workspace_id,
            "name": f"summary-m4-price-{label}",
            "effective_at": datetime.now(UTC),
            "hash": _hash(f"price-{label}"),
            "created_by": user_id,
        },
    )
    return {
        "label": label,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "agent_id": agent_id,
        "version_id": version_id,
        "thread_id": thread_id,
        "run_id": run_id,
        "turn_id": turn_id,
        "pricing_id": pricing_id,
    }


def _job_parameters(
    tenant: dict[str, UUID | str],
    *,
    identity_hash: str | None = None,
) -> dict[str, object]:
    label = str(tenant["label"])
    return {
        "id": uuid4(),
        "workspace_id": tenant["workspace_id"],
        "thread_id": tenant["thread_id"],
        "trigger_run_id": tenant["run_id"],
        "identity_hash": identity_hash or _hash(f"job-{label}"),
        "source_watermark": 1,
        "source_scope": '{"kind": "COMPLETED_PREFIX", "through_sequence": 1}',
        "source_scope_hash": _hash(f"scope-{label}"),
        "source_content_hash": _hash(f"source-{label}"),
        "policy_spec": '{"schema_version": 1, "mode": "single_dispatch"}',
        "policy_version": "summary-policy-v1",
        "prompt_version": "summary-prompt-v1",
        "budget_version": "summary-budget-v1",
        "model_spec": '{"provider": "offline", "model": "offline-summary-model"}',
        "model_spec_hash": _hash("offline-summary-model"),
        "tokenizer_id": "offline-tokenizer",
        "tokenizer_version": "1",
        "target_budget_tokens": 1024,
        "permission_snapshot_hash": _hash(f"permissions-{label}"),
        "created_by": tenant["user_id"],
    }


def _insert_job(
    connection: Connection,
    tenant: dict[str, UUID | str],
    *,
    identity_hash: str | None = None,
) -> UUID:
    parameters = _job_parameters(tenant, identity_hash=identity_hash)
    connection.execute(
        text(
            """
            INSERT INTO thread_summary_jobs (
              id, workspace_id, thread_id, trigger_run_id, identity_hash,
              source_watermark, source_scope, source_scope_hash,
              source_content_hash, policy_spec, policy_version, prompt_version,
              budget_version, model_spec, model_spec_hash, tokenizer_id,
              tokenizer_version, target_budget_tokens, permission_snapshot_hash,
              created_by
            )
            VALUES (
              :id, :workspace_id, :thread_id, :trigger_run_id, :identity_hash,
              :source_watermark, CAST(:source_scope AS jsonb), :source_scope_hash,
              :source_content_hash, CAST(:policy_spec AS jsonb), :policy_version,
              :prompt_version, :budget_version, CAST(:model_spec AS jsonb),
              :model_spec_hash, :tokenizer_id, :tokenizer_version,
              :target_budget_tokens, :permission_snapshot_hash, :created_by
            )
            """
        ),
        parameters,
    )
    return parameters["id"]


def _commit_dispatch_intent(
    connection: Connection,
    tenant: dict[str, UUID | str],
    job_id: UUID,
) -> tuple[UUID, UUID]:
    dispatch_id = uuid4()
    reservation_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO thread_summary_dispatches (
              id, workspace_id, thread_id, job_id, request_hash, intent_hash,
              reservation_id, pricing_snapshot_id, reserved_input_tokens,
              reserved_output_tokens, reserved_cost_amount, reserved_currency,
              cost_is_estimate, budget_reservation, budget_reservation_hash
            )
            VALUES (
              :id, :workspace_id, :thread_id, :job_id, :request_hash,
              :intent_hash, :reservation_id, :pricing_snapshot_id, 1000, 512,
              0.00256, 'CNY', false,
              '{"input_tokens": 1000, "output_tokens": 512, "state": "RESERVED"}'::jsonb,
              :reservation_hash
            )
            """
        ),
        {
            "id": dispatch_id,
            "workspace_id": tenant["workspace_id"],
            "thread_id": tenant["thread_id"],
            "job_id": job_id,
            "request_hash": _hash(f"request-{job_id}"),
            "intent_hash": _hash(f"intent-{job_id}"),
            "reservation_id": reservation_id,
            "pricing_snapshot_id": tenant["pricing_id"],
            "reservation_hash": _hash(f"reservation-{job_id}"),
        },
    )
    connection.execute(
        text(
            """
            UPDATE thread_summary_jobs
            SET status = 'DISPATCH_INTENT',
                budget_reserved = true,
                dispatch_id = :dispatch_id,
                dispatch_intent_at = now()
            WHERE workspace_id = :workspace_id
              AND thread_id = :thread_id
              AND id = :job_id
              AND status = 'CLAIMED'
            """
        ),
        {
            "workspace_id": tenant["workspace_id"],
            "thread_id": tenant["thread_id"],
            "job_id": job_id,
            "dispatch_id": dispatch_id,
        },
    )
    return dispatch_id, reservation_id


def _claim_job(
    connection: Connection,
    tenant: dict[str, UUID | str],
    job_id: UUID,
) -> None:
    connection.execute(
        text(
            """
            UPDATE thread_summary_jobs
            SET status = 'CLAIMED', lease_token = :lease_token,
                lease_expires_at = now() + interval '5 minutes', claimed_at = now()
            WHERE id = :id
            """
        ),
        {
            "id": job_id,
            "lease_token": f"lease-{job_id}",
        },
    )
    del tenant


def test_migration_installs_version_tables_constraints_and_triggers(
    connection: Connection,
) -> None:
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
        "0042_summary_eligibility"
    )
    assert set(
        connection.execute(
            text(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public' AND tablename LIKE 'thread_summary_%'
                """
            )
        ).scalars()
    ) == SUMMARY_TABLES
    constraints = set(
        connection.execute(
            text(
                """
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'thread_summary_jobs'::regclass
                """
            )
        ).scalars()
    )
    assert {
        "uq_thread_summary_jobs_identity",
        "fk_thread_summary_jobs_thread_workspace",
        "fk_thread_summary_jobs_run_workspace",
        "fk_thread_summary_jobs_dispatch_scope",
        "fk_thread_summary_jobs_response_scope",
        "fk_thread_summary_jobs_artifact_scope",
    } <= constraints
    triggers = set(
        connection.execute(
            text(
                """
                SELECT DISTINCT trigger_name FROM information_schema.triggers
                WHERE trigger_schema = 'public'
                  AND event_object_table LIKE 'thread_summary_%'
                """
            )
        ).scalars()
    )
    assert {
        "thread_summary_job_scope_guard",
        "thread_summary_source_guard",
        "thread_summary_dispatch_guard",
        "thread_summary_dispatch_commit_guard",
        "thread_summary_response_guard",
        "thread_summary_artifact_guard",
    } <= triggers


def test_identity_deduplication_and_dispatch_pricing_composite_fk(
    connection: Connection,
) -> None:
    tenant = _seed_tenant(connection)
    other = _seed_tenant(connection)
    identity = _hash("shared-summary-identity")
    _insert_job(connection, tenant, identity_hash=identity)

    duplicate = _job_parameters(tenant, identity_hash=identity)
    connection.execute(text("SAVEPOINT duplicate_job"))
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                """
                INSERT INTO thread_summary_jobs (
                  id, workspace_id, thread_id, trigger_run_id, identity_hash,
                  source_watermark, source_scope, source_scope_hash,
                  source_content_hash, policy_spec, policy_version, prompt_version,
                  budget_version, model_spec, model_spec_hash, tokenizer_id,
                  tokenizer_version, target_budget_tokens, permission_snapshot_hash,
                  created_by
                )
                VALUES (
                  :id, :workspace_id, :thread_id, :trigger_run_id, :identity_hash,
                  :source_watermark, CAST(:source_scope AS jsonb), :source_scope_hash,
                  :source_content_hash, CAST(:policy_spec AS jsonb), :policy_version,
                  :prompt_version, :budget_version, CAST(:model_spec AS jsonb),
                  :model_spec_hash, :tokenizer_id, :tokenizer_version,
                  :target_budget_tokens, :permission_snapshot_hash, :created_by
                )
                """
            ),
            duplicate | {"id": uuid4()},
        )
    connection.execute(text("ROLLBACK TO SAVEPOINT duplicate_job"))

    job_id = _insert_job(connection, tenant)
    _claim_job(connection, tenant, job_id)
    cross_tenant_dispatch = {
        "id": uuid4(),
        "workspace_id": tenant["workspace_id"],
        "thread_id": tenant["thread_id"],
        "job_id": job_id,
        "request_hash": _hash(f"cross-tenant-request-{job_id}"),
        "intent_hash": _hash(f"cross-tenant-intent-{job_id}"),
        "reservation_id": uuid4(),
        "pricing_snapshot_id": other["pricing_id"],
        "reservation_hash": _hash(f"cross-tenant-reservation-{job_id}"),
    }
    error = _expect_db_error(
        connection,
        """
        INSERT INTO thread_summary_dispatches (
          id, workspace_id, thread_id, job_id, request_hash, intent_hash,
          reservation_id, pricing_snapshot_id, reserved_input_tokens,
          reserved_output_tokens, budget_reservation, budget_reservation_hash
        )
        VALUES (
          :id, :workspace_id, :thread_id, :job_id, :request_hash,
          :intent_hash, :reservation_id, :pricing_snapshot_id, 1000, 512,
          '{"state": "RESERVED"}'::jsonb, :reservation_hash
        )
        """,
        cross_tenant_dispatch,
        kind=IntegrityError,
    )
    assert "fk_thread_summary_dispatches_pricing_workspace" in str(error)


def test_dispatch_intent_and_budget_reservation_commit_together(
    connection: Connection,
) -> None:
    non_atomic_tenant = _seed_tenant(connection)
    non_atomic_job_id = _insert_job(connection, non_atomic_tenant)
    _claim_job(connection, non_atomic_tenant, non_atomic_job_id)
    dispatch_id = uuid4()
    connection.execute(text("ROLLBACK"))
    connection.execute(text("SAVEPOINT after_non_atomic_dispatch"))
    non_atomic_tenant = _seed_tenant(connection)
    non_atomic_job_id = _insert_job(connection, non_atomic_tenant)
    _claim_job(connection, non_atomic_tenant, non_atomic_job_id)
    _assert_deferred_failure_commits(
        connection,
        """
        INSERT INTO thread_summary_dispatches (
          id, workspace_id, thread_id, job_id, request_hash, intent_hash,
          reservation_id, pricing_snapshot_id, reserved_input_tokens,
          reserved_output_tokens, budget_reservation,
          budget_reservation_hash
        )
        VALUES (
          :id, :workspace_id, :thread_id, :job_id, :request_hash,
          :intent_hash, :reservation_id, :pricing_snapshot_id,
          1000, 512, '{"state": "RESERVED"}'::jsonb, :reservation_hash
        )
        """,
        {
            "id": dispatch_id,
            "workspace_id": non_atomic_tenant["workspace_id"],
            "thread_id": non_atomic_tenant["thread_id"],
            "job_id": non_atomic_job_id,
            "request_hash": _hash(f"separate-request-{non_atomic_job_id}"),
            "intent_hash": _hash(f"separate-intent-{non_atomic_job_id}"),
            "reservation_id": uuid4(),
            "pricing_snapshot_id": non_atomic_tenant["pricing_id"],
            "reservation_hash": _hash(f"separate-reservation-{non_atomic_job_id}"),
        },
    )

    tenant = _seed_tenant(connection)
    job_id = _insert_job(connection, tenant)
    _claim_job(connection, tenant, job_id)
    _commit_dispatch_intent(connection, tenant, job_id)
    row = connection.execute(
        text(
            """
            SELECT job.status, job.budget_reserved, job.dispatch_id,
                   dispatch.id AS ledger_dispatch_id, dispatch.reservation_id
            FROM thread_summary_jobs AS job
            JOIN thread_summary_dispatches AS dispatch
              ON dispatch.workspace_id = job.workspace_id
             AND dispatch.thread_id = job.thread_id
             AND dispatch.job_id = job.id
             AND dispatch.id = job.dispatch_id
            WHERE job.id = :job_id
            """
        ),
        {"job_id": job_id},
    ).one()
    assert row.status == "DISPATCH_INTENT"
    assert row.budget_reserved is True
    assert row.dispatch_id == row.ledger_dispatch_id
    assert row.reservation_id

    connection.execute(text("SAVEPOINT unique_dispatch"))
    duplicate_dispatch = {
        "id": uuid4(),
        "workspace_id": tenant["workspace_id"],
        "thread_id": tenant["thread_id"],
        "job_id": job_id,
        "request_hash": _hash("duplicate-request"),
        "intent_hash": _hash("duplicate-intent"),
        "reservation_id": uuid4(),
        "reservation_hash": _hash("duplicate-reservation"),
    }
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                """
                INSERT INTO thread_summary_dispatches (
                  id, workspace_id, thread_id, job_id, request_hash, intent_hash,
                  reservation_id, reserved_input_tokens, reserved_output_tokens,
                  budget_reservation, budget_reservation_hash
                )
                VALUES (
                  :id, :workspace_id, :thread_id, :job_id, :request_hash,
                  :intent_hash, :reservation_id, 1, 1,
                  '{"state": "RETRY"}'::jsonb, :reservation_hash
                )
                """
            ),
            duplicate_dispatch,
        )
    connection.execute(text("ROLLBACK TO SAVEPOINT unique_dispatch"))


def _store_response(
    connection: Connection,
    tenant: dict[str, UUID | str],
    job_id: UUID,
    dispatch_id: UUID,
) -> UUID:
    response_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO thread_summary_responses (
              id, workspace_id, thread_id, job_id, dispatch_id, provider, model,
              response_sha256, raw_response, input_tokens, output_tokens,
              total_tokens, finish_reason
            )
            VALUES (
              :id, :workspace_id, :thread_id, :job_id, :dispatch_id,
              'offline', 'offline-summary-model', :response_hash,
              '{"status": "SUFFICIENT", "goal": "persist evidence"}'::jsonb,
              1000, 120, 1120, 'stop'
            )
            """
        ),
        {
            "id": response_id,
            "workspace_id": tenant["workspace_id"],
            "thread_id": tenant["thread_id"],
            "job_id": job_id,
            "dispatch_id": dispatch_id,
            "response_hash": _hash(f"response-{job_id}"),
        },
    )
    connection.execute(
        text(
            """
            UPDATE thread_summary_jobs
            SET status = 'RESPONSE_STORED', response_id = :response_id,
                response_stored_at = now(), lease_token = NULL,
                lease_expires_at = NULL
            WHERE id = :job_id AND status = 'DISPATCH_INTENT'
            """
        ),
        {"response_id": response_id, "job_id": job_id},
    )
    connection.execute(
        text(
            """
            UPDATE thread_summary_dispatches
            SET outcome = 'RESPONSE_RECEIVED', settled_at = now()
            WHERE id = :dispatch_id AND outcome = 'DISPATCHED'
            """
        ),
        {"dispatch_id": dispatch_id},
    )
    return response_id


def _store_pending_artifact(
    connection: Connection,
    tenant: dict[str, UUID | str],
    job_id: UUID,
    response_id: UUID,
) -> UUID:
    artifact_id = uuid4()
    content_hash = _hash(f"summary-content-{job_id}")
    connection.execute(
        text(
            """
            INSERT INTO thread_summary_artifacts (
              id, workspace_id, thread_id, job_id, response_id, revision_number,
              revision_hash, content_hash, summary_identity_hash, summary_content,
              source_index, source_index_hash, validator_version, validation_result
            )
            VALUES (
              :id, :workspace_id, :thread_id, :job_id, :response_id, 1,
              :revision_hash, :content_hash, :identity_hash,
              '{"status": "SUFFICIENT", "goal": "persist evidence"}'::jsonb,
              '[{"source_id": "turn-1", "run_id": "run-1"}]'::jsonb,
              :source_hash, 'summary-validator-v1', '{"valid": true}'::jsonb
            )
            """
        ),
        {
            "id": artifact_id,
            "workspace_id": tenant["workspace_id"],
            "thread_id": tenant["thread_id"],
            "job_id": job_id,
            "response_id": response_id,
            "revision_hash": _hash(f"revision-{job_id}"),
            "content_hash": content_hash,
            "identity_hash": content_hash,
            "source_hash": _hash(f"source-index-{job_id}"),
        },
    )
    connection.execute(
        text(
            """
            UPDATE thread_summary_jobs
            SET status = 'VALIDATED', artifact_id = :artifact_id,
                validation_result = '{"valid": true}'::jsonb, validated_at = now()
            WHERE id = :job_id AND status = 'RESPONSE_STORED'
            """
        ),
        {"artifact_id": artifact_id, "job_id": job_id},
    )
    return artifact_id


def test_response_and_artifact_content_are_immutable_with_append_only_publication(
    connection: Connection,
) -> None:
    tenant = _seed_tenant(connection)
    job_id = _insert_job(connection, tenant)
    _claim_job(connection, tenant, job_id)
    dispatch_id, _ = _commit_dispatch_intent(connection, tenant, job_id)
    connection.execute(
        text(
            """
            UPDATE thread_summary_dispatches
            SET outcome = 'DISPATCHED', dispatched_at = now()
            WHERE id = :dispatch_id
            """
        ),
        {"dispatch_id": dispatch_id},
    )
    response_id = _store_response(connection, tenant, job_id, dispatch_id)
    savepoint = connection.begin_nested()
    with pytest.raises(DBAPIError):
        connection.execute(
            text("UPDATE thread_summary_responses SET raw_response = '{}'::jsonb WHERE id = :id"),
            {"id": response_id},
        )
    savepoint.rollback()

    artifact_id = _store_pending_artifact(connection, tenant, job_id, response_id)
    savepoint = connection.begin_nested()
    with pytest.raises(DBAPIError):
        connection.execute(
            text(
                """
                UPDATE thread_summary_artifacts
                SET summary_content = '{"tampered": true}'::jsonb
                WHERE id = :id
                """
            ),
            {"id": artifact_id},
        )
    savepoint.rollback()

    connection.execute(
        text(
            """
            UPDATE thread_summary_artifacts
            SET publication_status = 'PUBLISHED', published_at = now(),
                published_by = :published_by
            WHERE id = :id AND publication_status = 'PENDING'
            """
        ),
        {"id": artifact_id, "published_by": "summary-publisher-v1"},
    )
    connection.execute(
        text(
            """
            UPDATE thread_summary_jobs
            SET status = 'PUBLISHED', published_at = now(), completed_at = now()
            WHERE id = :job_id AND status = 'VALIDATED'
            """
        ),
        {"job_id": job_id},
    )
    savepoint = connection.begin_nested()
    with pytest.raises(DBAPIError):
        connection.execute(
            text(
                """
                UPDATE thread_summary_artifacts
                SET publication_status = 'STALE'
                WHERE id = :id
                """
            ),
            {"id": artifact_id},
        )
    savepoint.rollback()

    row = connection.execute(
        text(
            """
            SELECT job.status, artifact.publication_status,
                   artifact.summary_content, response.raw_response
            FROM thread_summary_jobs AS job
            JOIN thread_summary_artifacts AS artifact
              ON artifact.workspace_id = job.workspace_id
             AND artifact.thread_id = job.thread_id
             AND artifact.job_id = job.id
            JOIN thread_summary_responses AS response
              ON response.workspace_id = job.workspace_id
             AND response.thread_id = job.thread_id
             AND response.job_id = job.id
            WHERE job.id = :job_id
            """
        ),
        {"job_id": job_id},
    ).one()
    assert row.status == "PUBLISHED"
    assert row.publication_status == "PUBLISHED"
    assert row.summary_content["goal"] == "persist evidence"
    assert row.raw_response["status"] == "SUFFICIENT"


def test_unknown_outcome_is_persisted_and_cannot_become_retryable(
    connection: Connection,
) -> None:
    tenant = _seed_tenant(connection)
    identity = _hash("unknown-terminal-identity")
    job_id = _insert_job(connection, tenant, identity_hash=identity)
    _claim_job(connection, tenant, job_id)
    dispatch_id, _ = _commit_dispatch_intent(connection, tenant, job_id)
    connection.execute(
        text(
            """
            UPDATE thread_summary_dispatches
            SET outcome = 'DISPATCHED', dispatched_at = now()
            WHERE id = :dispatch_id
            """
        ),
        {"dispatch_id": dispatch_id},
    )
    connection.execute(
        text(
            """
            UPDATE thread_summary_jobs
            SET status = 'UNKNOWN_OUTCOME', lease_token = NULL,
                lease_expires_at = NULL, completed_at = now()
            WHERE id = :job_id
            """
        ),
        {"job_id": job_id},
    )
    connection.execute(
        text(
            """
            UPDATE thread_summary_dispatches
            SET outcome = 'UNKNOWN_OUTCOME', settled_at = now()
            WHERE id = :dispatch_id
            """
        ),
        {"dispatch_id": dispatch_id},
    )
    connection.execute(text("SAVEPOINT unknown_terminal"))
    with pytest.raises(DBAPIError):
        connection.execute(
            text(
                """
                UPDATE thread_summary_jobs SET status = 'FAILED'
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id},
        )
    connection.execute(text("ROLLBACK TO SAVEPOINT unknown_terminal"))

    connection.execute(text("SAVEPOINT unknown_dispatch"))
    with pytest.raises(DBAPIError):
        connection.execute(
            text(
                """
                UPDATE thread_summary_dispatches
                SET outcome = 'FAILED_NOT_DISPATCHED'
                WHERE id = :dispatch_id
                """
            ),
            {"dispatch_id": dispatch_id},
        )
    connection.execute(text("ROLLBACK TO SAVEPOINT unknown_dispatch"))

    duplicate = _job_parameters(tenant, identity_hash=identity)
    connection.execute(text("SAVEPOINT unknown_reinsert"))
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                """
                INSERT INTO thread_summary_jobs (
                  id, workspace_id, thread_id, trigger_run_id, identity_hash,
                  source_watermark, source_scope, source_scope_hash,
                  source_content_hash, policy_spec, policy_version, prompt_version,
                  budget_version, model_spec, model_spec_hash, tokenizer_id,
                  tokenizer_version, target_budget_tokens, permission_snapshot_hash,
                  created_by
                )
                VALUES (
                  :id, :workspace_id, :thread_id, :trigger_run_id, :identity_hash,
                  :source_watermark, CAST(:source_scope AS jsonb), :source_scope_hash,
                  :source_content_hash, CAST(:policy_spec AS jsonb), :policy_version,
                  :prompt_version, :budget_version, CAST(:model_spec AS jsonb),
                  :model_spec_hash, :tokenizer_id, :tokenizer_version,
                  :target_budget_tokens, :permission_snapshot_hash, :created_by
                )
                """
            ),
            duplicate | {"id": uuid4()},
        )
    connection.execute(text("ROLLBACK TO SAVEPOINT unknown_reinsert"))


def test_source_manifest_stores_identities_hashes_and_rejects_wrong_thread_binding(
    connection: Connection,
) -> None:
    tenant = _seed_tenant(connection)
    other = _seed_tenant(connection)
    job_id = _insert_job(connection, tenant)
    columns = set(
        connection.execute(
            text(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'thread_summary_job_sources'
                """
            )
        ).scalars()
    )
    assert not {"user_input", "input_text", "final_output", "raw_text"} & columns

    source = {
        "id": uuid4(),
        "workspace_id": tenant["workspace_id"],
        "thread_id": tenant["thread_id"],
        "job_id": job_id,
        "ordinal": 1,
        "source_id": f"source-{tenant['label']}",
        "turn_id": tenant["turn_id"],
        "run_id": tenant["run_id"],
        "sequence": 1,
        "original_ref": "thread_turns/1",
        "source_identity_hash": _hash(f"source-{tenant['label']}"),
        "revision_sha256": _hash(f"revision-{tenant['label']}"),
        "user_input_sha256": _hash(f"raw source question {tenant['label']}"),
        "final_output_sha256": _hash(f"raw source answer {tenant['label']}"),
        "eligibility_reason": "completed_exchange",
    }
    connection.execute(
        text(
            """
            INSERT INTO thread_summary_job_sources (
              id, workspace_id, thread_id, job_id, ordinal, source_id,
              turn_id, run_id, sequence, original_ref, source_identity_hash,
              revision_sha256, user_input_sha256, final_output_sha256,
              summary_eligibility, eligibility_reason
            )
            VALUES (
              :id, :workspace_id, :thread_id, :job_id, :ordinal, :source_id,
              :turn_id, :run_id, :sequence, :original_ref, :source_identity_hash,
              :revision_sha256, :user_input_sha256, :final_output_sha256,
              'ELIGIBLE', :eligibility_reason
            )
            """
        ),
        source,
    )
    wrong_thread = source | {
        "id": uuid4(),
        "workspace_id": other["workspace_id"],
        "thread_id": other["thread_id"],
        "job_id": _insert_job(connection, other),
        "turn_id": tenant["turn_id"],
        "run_id": tenant["run_id"],
        "source_id": f"wrong-source-{tenant['label']}",
    }
    _expect_db_error(
        connection,
        """
        INSERT INTO thread_summary_job_sources (
          id, workspace_id, thread_id, job_id, ordinal, source_id,
          turn_id, run_id, sequence, original_ref, source_identity_hash,
          revision_sha256, user_input_sha256, final_output_sha256,
          summary_eligibility, eligibility_reason
        )
        VALUES (
          :id, :workspace_id, :thread_id, :job_id, :ordinal, :source_id,
          :turn_id, :run_id, :sequence, :original_ref, :source_identity_hash,
          :revision_sha256, :user_input_sha256, :final_output_sha256,
          'ELIGIBLE', :eligibility_reason
        )
        """,
        wrong_thread,
    )
