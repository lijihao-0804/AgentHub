"""Rehearse 0056/0057 in a fresh loopback test DB; cleanup is the default."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from packages.agent_runtime.models import AgentRun
from packages.approvals.models import Approval
from packages.core.database import create_database
from tests.integration.test_auxiliary_run_step_migration import _migrate
from tests.integration.test_m4c_agent_runtime import _seed

ADMIN = os.environ.get("AGENTHUB_TEST_ADMIN_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not ADMIN, reason="Local test admin URL required"),
]


def test_upgrade_preserves_legacy_decisions_and_downgrade_refuses_to_erase_delegation():
    admin = make_url(ADMIN)
    assert admin.host in {"127.0.0.1", "localhost", "::1"}
    name = "agenthub_delegation_test_" + uuid4().hex
    admin = admin.set(drivername="postgresql+psycopg", database="postgres")
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = admin.set(database=name)
    engine = create_engine(url)

    async def seed_legacy_rows():
        db, factory = create_database(
            url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        )
        try:
            async with factory() as session:
                base = await _seed(session, label=uuid4().hex)
                run = AgentRun(
                    workspace_id=base["workspace_id"],
                    agent_version_id=base["version"].id,
                    input_text="Synthetic",
                    created_by=base["user"].id,
                )
                session.add(run)
                await session.flush()
                for status in ("APPROVED", "PENDING"):
                    key = uuid4().hex
                    # Only write columns available on the deployed 0056 schema.
                    await session.execute(
                        insert(Approval.__table__).values(
                            id=uuid4(),
                            workspace_id=run.workspace_id,
                            run_id=run.id,
                            agent_version_id=run.agent_version_id,
                            logical_action_id=key,
                            idempotency_key=key,
                            tool_identity="create_ticket",
                            canonical_arguments={},
                            canonical_args_hash="0" * 64,
                            requested_by=run.created_by,
                            decision_status=status,
                            decided_by=run.created_by if status == "APPROVED" else None,
                            decided_at=datetime.now(UTC) if status == "APPROVED" else None,
                        )
                    )
                await session.commit()
        finally:
            await db.dispose()

    try:
        _migrate(url, "0056_auxiliary_run_steps")
        asyncio.run(seed_legacy_rows())
        _migrate(url, "0057_delegated_approvals")
        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT decision_status,decision_source,routing_reason "
                    "FROM approvals ORDER BY decision_status"
                )
            ).all() == [("APPROVED", "HUMAN", None), ("PENDING", None, None)]
        _migrate(url, "0056_auxiliary_run_steps", downgrade=True)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM approvals")) == 2
        _migrate(url, "0057_delegated_approvals")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE approvals SET decision_source='DELEGATED', "
                    "routing_reason='WITHIN_LEVEL' WHERE decision_status='APPROVED'"
                )
            )
        with pytest.raises(DBAPIError, match="0057 downgrade refused"):
            _migrate(url, "0056_auxiliary_run_steps", downgrade=True)
        with engine.connect() as conn:
            assert (
                conn.scalar(text("SELECT version_num FROM alembic_version"))
                == "0057_delegated_approvals"
            )
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM approvals WHERE decision_source='DELEGATED'")
                )
                == 1
            )
    finally:
        engine.dispose()
        try:
            if os.environ.get("AGENTHUB_KEEP_APPROVAL_MIGRATION_TEST_DB") == "1":
                # An explicitly retained test DB can be inspected on hosts
                # that cannot signal PostgreSQL's checkpointer for DROP.
                print(f"retained_test_database={name}; revision_boundary=0056/0057")
            else:
                with admin_engine.connect() as conn:
                    conn.exec_driver_sql(f'DROP DATABASE "{name}"')
        finally:
            admin_engine.dispose()
