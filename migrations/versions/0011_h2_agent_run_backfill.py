"""Backfill immutable AgentRun identity from historical AgentVersion rows.

Malformed or incomplete historical snapshot projections intentionally remain ``[]``;
the migration never invents a snapshot identity.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_h2_agent_run_backfill"
down_revision: str | None = "0010_h2_semantic_closure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _effective_snapshots(resolved_spec: Any, schema_version: Any) -> list[dict[str, str]]:
    if schema_version != 1 or not isinstance(resolved_spec, Mapping):
        return []
    retrieval = resolved_spec.get("retrieval")
    if not isinstance(retrieval, Mapping):
        return []
    snapshots = retrieval.get("knowledge_snapshots")
    if not isinstance(snapshots, list):
        return []
    projection: list[dict[str, str]] = []
    for item in snapshots:
        if not isinstance(item, Mapping):
            return []
        snapshot_id = item.get("snapshot_id")
        snapshot_hash = item.get("snapshot_hash")
        if (
            not isinstance(snapshot_id, str)
            or not snapshot_id
            or snapshot_id == "LATEST"
            or not isinstance(snapshot_hash, str)
            or not snapshot_hash
        ):
            return []
        projection.append({"snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash})
    return projection


def backfill_agent_runs(connection: sa.Connection) -> None:
    agent_runs = sa.table(
        "agent_runs",
        sa.column("id"),
        sa.column("resolved_spec_hash"),
        sa.column("effective_knowledge_snapshots", postgresql.JSONB()),
        sa.column("agent_version_id"),
        sa.column("workspace_id"),
    )
    agent_versions = sa.table(
        "agent_versions",
        sa.column("id"),
        sa.column("workspace_id"),
        sa.column("spec_schema_version"),
        sa.column("resolved_spec", postgresql.JSONB()),
        sa.column("resolved_spec_hash"),
    )
    rows = connection.execute(
        sa.select(
            agent_runs.c.id,
            agent_runs.c.effective_knowledge_snapshots,
            agent_versions.c.spec_schema_version,
            agent_versions.c.resolved_spec,
            agent_versions.c.resolved_spec_hash,
        )
        .select_from(
            agent_runs.join(
                agent_versions,
                (agent_runs.c.workspace_id == agent_versions.c.workspace_id)
                & (agent_runs.c.agent_version_id == agent_versions.c.id),
            )
        )
        .where(agent_runs.c.resolved_spec_hash.is_(None))
    ).mappings()

    for row in rows:
        existing = row["effective_knowledge_snapshots"]
        effective = (
            existing
            if isinstance(existing, list) and existing
            else _effective_snapshots(row["resolved_spec"], row["spec_schema_version"])
        )
        connection.execute(
            agent_runs.update()
            .where(agent_runs.c.id == row["id"])
            .values(
                resolved_spec_hash=row["resolved_spec_hash"],
                effective_knowledge_snapshots=effective,
            )
        )


def upgrade() -> None:
    backfill_agent_runs(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("0011 agent run backfill is data-only and cannot be safely reversed.")
