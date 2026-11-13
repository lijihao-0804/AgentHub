"""Exercise the operator routes through real membership, transactions and PostgreSQL."""

import asyncio
import os
from importlib import import_module
from uuid import UUID

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from apps.api.app import create_app
from packages.agent_runtime.models import Agent
from packages.control_plane.models import (
    AuditLog,
    OrganizationMembership,
    User,
    WorkspaceMembership,
)
from packages.core.config.settings import Settings
from packages.tools.actions import CreateTicketActionExecutor
from packages.tools.models import Customer, Ticket
from tests.integration.test_ae10_evidence_view import _client
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database

__all__ = ["db_factory", "migrated_database"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="PostgreSQL required"
    ),
]


async def test_0055_preserves_existing_records_and_marks_historical_fixtures(db_factory):
    # Temporary tables shadow the actual tables on this connection. Exercise the
    # real migration both ways without changing the schema version of the test DB.
    migration = import_module("migrations.versions.0055_support_records")
    async with db_factory() as session, session.begin():
        conn = await session.connection()
        for statement in (
            "CREATE TEMP TABLE customers (id int, workspace_id int, customer_ref text, "
            "name text, email text) ON COMMIT DROP",
            "CREATE TEMP TABLE tickets (id int, status text) ON COMMIT DROP",
            "CREATE TEMP TABLE evaluation_interactive_episodes (workspace_id int, customer_id int) "
            "ON COMMIT DROP",
            "INSERT INTO customers VALUES (1, 1, 'R', 'Original', NULL), "
            "(2, 1, 'R2', 'Controlled', NULL), "
            "(3, 1, 'CUSTOM-abcdef0123456789abcdef01', 'AE6B Fixture abcdef01', "
            "'custom-abcdef0123456789abcdef01@example.test'), "
            "(4, 1, 'BUSINESS-abcdef0123456789abcdef01', 'Original', NULL), "
            "(2, 2, 'R2', 'Other tenant', NULL)",
            "INSERT INTO evaluation_interactive_episodes VALUES (1,2)",
            "INSERT INTO tickets VALUES (1,'PENDING'),(2,'CLOSED')",
        ):
            await conn.execute(text(statement))

        def apply(connection, operation):
            with Operations.context(MigrationContext.configure(connection)):
                operation()

        await conn.run_sync(apply, migration.upgrade)
        fixture_ids = (
            (
                await conn.execute(
                    text("SELECT id FROM customers WHERE is_evaluation_fixture ORDER BY id")
                )
            )
            .scalars()
            .all()
        )
        assert fixture_ids == [2, 3]
        assert (
            await conn.execute(text("SELECT status, version FROM tickets ORDER BY id"))
        ).all() == [("PENDING", 1), ("CLOSED", 1)]
        await conn.run_sync(apply, migration.downgrade)
        assert (await conn.execute(text("SELECT count(*) FROM customers"))).scalar_one() == 5


async def test_customer_registration_and_ticket_concurrency(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label="support-records")
        other = await _seed(session, label="separate-support-records")
        workspace = base["workspace_id"]
        email = f"viewer-{workspace}@example.test"
        viewer_user = User(email=email, normalized_email=email, password_hash="unused")
        session.add(viewer_user)
        await session.flush()
        session.add_all(
            [
                OrganizationMembership(
                    organization_id=base["organization_id"], user_id=viewer_user.id, role="MEMBER"
                ),
                WorkspaceMembership(workspace_id=workspace, user_id=viewer_user.id, role="VIEWER"),
            ]
        )
        await session.commit()
    settings = Settings(testing=True)
    app = create_app(settings)
    app.state.db_session_factory = db_factory
    scope = f"/api/v1/workspaces/{workspace}"
    root = scope + "/support"
    body = {"customer_ref": "  REF_%  ", "name": " Example ", "email": "OP@EXAMPLE.TEST"}
    async with (
        _client(app, base["user"].id, settings) as owner,
        _client(app, viewer_user.id, settings) as viewer,
        _client(app, other["user"].id, settings) as stranger,
    ):
        creates = await asyncio.gather(
            *(owner.post(root + "/customers", json=body) for _ in range(4))
        )
        assert sorted(r.status_code for r in creates) == [200, 200, 200, 201]
        [customer_id] = {r.json()["id"] for r in creates}
        record = creates[0].json()
        assert (record["customer_ref"], record["name"], record["email"]) == (
            "REF_%",
            "Example",
            "op@example.test",
        )
        conflict = await owner.post(root + "/customers", json=body | {"name": "different"})
        assert (
            conflict.status_code == 409
            and conflict.json()["error"]["code"] == "CUSTOMER_REF_CONFLICT"
        )
        assert (
            await owner.post(root + "/customers", json=body | {"name": "  "})
        ).status_code == 422
        assert (
            await owner.post(root + "/customers", json=body | {"name": "unusable\x00name"})
        ).status_code == 422
        second = await owner.post(
            root + "/customers", json={"customer_ref": "REF1234", "name": "Second"}
        )
        assert second.status_code == 201
        async with db_factory() as session:
            fixture = Customer(
                workspace_id=workspace,
                customer_ref="private-fixture",
                name="Private",
                is_evaluation_fixture=True,
            )
            draft = Agent(
                workspace_id=workspace,
                name="draft",
                system_prompt="draft",
                model_profile_id=base["profile"].id,
            )
            session.add_all([fixture, draft])
            await session.flush()
            ticket = Ticket(
                workspace_id=workspace,
                customer_id=UUID(customer_id),
                ticket_ref="T-1",
                subject="Example subject",
                idempotency_key="original-support-action",
            )
            private_ticket = Ticket(
                workspace_id=workspace,
                customer_id=fixture.id,
                ticket_ref="TEST-1",
                subject="Example fixture",
            )
            session.add_all([ticket, private_ticket])
            await session.commit()
        published = await owner.get(scope + "/agents", params={"published_only": "true"})
        assert [a["id"] for a in published.json()] == [str(base["version"].agent_id)]
        assert str(draft.id) in {a["id"] for a in (await owner.get(scope + "/agents")).json()}
        page = await owner.get(root + "/customers", params={"q": "_%"})
        assert page.json() == {"items": [record], "total": 1}
        last = await owner.get(root + "/customers", params={"limit": 1, "offset": 1})
        assert last.json() == {"items": [record], "total": 2}
        detail = root + "/tickets/" + str(ticket.id)
        case_path = root + f"/customers/{customer_id}/tickets"
        assert (await owner.get(case_path)).json()["items"][0]["version"] == 1
        assert (await owner.get(detail)).json()["customer_name"] == record["name"]
        for path in (root + "/customers", case_path, detail):
            assert (await viewer.get(path)).status_code == 403
            assert (await stranger.get(path)).status_code in (403, 404)
        assert (await viewer.post(root + "/customers", json=body)).status_code == 403
        update = {"expected_version": 1, "status": "PENDING"}
        assert (await viewer.patch(detail, json=update)).status_code == 403
        for suffix in (f"/customers/{fixture.id}/tickets", f"/tickets/{private_ticket.id}"):
            assert (await owner.get(root + suffix)).status_code == 404
        assert (
            await owner.patch(root + f"/tickets/{private_ticket.id}", json=update)
        ).status_code == 404
        other_root = f"/api/v1/workspaces/{other['workspace_id']}/support"
        assert (await stranger.get(other_root + f"/tickets/{ticket.id}")).status_code == 404
        assert (
            await stranger.patch(other_root + f"/tickets/{ticket.id}", json=update)
        ).status_code == 404
        writes = await asyncio.gather(
            owner.patch(detail, json=update),
            owner.patch(detail, json=update | {"status": "IN_PROGRESS"}),
        )
        assert sorted(r.status_code for r in writes) == [200, 409]
        winner = next(r.json() for r in writes if r.status_code == 200)
        assert winner["version"] == 2
        assert (
            await owner.patch(detail, json=update | {"status": winner["status"]})
        ).json() == winner
        close = {"expected_version": 2, "status": "CLOSED"}
        closed = (await owner.patch(detail, json=close)).json()
        assert closed["version"] == 3 and closed["status"] == "CLOSED"
        assert (await owner.patch(detail, json=close)).json() == closed
        assert (await owner.patch(detail, json=update)).status_code == 409
        assert (
            await owner.patch(detail, json={"expected_version": 3, "status": "PENDING"})
        ).status_code == 409
        assert (await owner.get(detail)).json() == closed
        executor = CreateTicketActionExecutor(db_factory)
        historical = await executor.reconcile(
            base["context"], idempotency_key="original-support-action"
        )
        assert historical.data == {
            "ticket_id": str(ticket.id),
            "ticket_ref": "T-1",
            "status": "OPEN",
        }
        invalid = await executor.execute(
            base["context"],
            None,
            {"customer_ref": record["customer_ref"], "subject": "X" * 501},
            idempotency_key="invalid-input",
        )
        assert invalid.failure_code == "TOOL_ARGUMENT_INVALID"
        assert (await owner.post(root + "/tickets", json={})).status_code in (404, 405)
    async with db_factory() as session:
        rows = list(
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.workspace_id == workspace, AuditLog.action.startswith("support."))
                .order_by(AuditLog.created_at)
            )
        )
        assert [r.action for r in rows] == ["support.customer.created"] * 2 + [
            "support.ticket.status_changed"
        ] * 2
        assert all(r.actor_user_id == base["user"].id and r.request_id for r in rows)
        assert [r.safe_metadata for r in rows] == [
            {},
            {},
            {"previous_status": "OPEN", "status": winner["status"], "version": 2},
            {"previous_status": winner["status"], "status": "CLOSED", "version": 3},
        ]
