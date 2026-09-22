"""Scope the four composite SET NULL foreign keys to the provenance column.

Every one of these constraints is ``(workspace_id, <something>)`` referencing a
table keyed the same way, so that a child row can never point at a parent in a
different tenant. That part works. The ``ON DELETE SET NULL`` action does not:
PostgreSQL nulls *all* the referencing columns, and ``workspace_id`` is NOT NULL
on all four tables. Deleting a parent therefore did not orphan the child -- it
raised ``NotNullViolationError`` and the delete failed.

The user-visible symptom was ``DELETE /threads/{id}`` returning 500 for any
thread that had ever been run, because ``agent_runs`` carries the same shape.
Two of the four constraints came in with 0028; the other two are older and were
broken the whole time, which is why this migration fixes all four rather than
only the memory ones -- leaving the thread delete broken would leave long-term
memory with no way to clean up after itself.

PostgreSQL 15 added ``ON DELETE SET NULL (column_list)``, which nulls only the
columns named. That is exactly the intent: forget which run taught this, keep
knowing whose workspace it is.
"""

from collections.abc import Sequence

from alembic import op

# Kept within 32 characters: alembic_version.version_num is varchar(32).
revision: str = "0029_set_null_columns"
down_revision: str | None = "0028_workspace_memories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, constraint, referenced table, referencing columns, column to null)
_CONSTRAINTS = (
    (
        "agent_runs",
        "fk_agent_runs_thread_workspace",
        "agent_threads",
        ("workspace_id", "thread_id"),
        "thread_id",
    ),
    (
        "artifacts",
        "fk_artifacts_run_workspace",
        "agent_runs",
        ("workspace_id", "run_id"),
        "run_id",
    ),
    (
        "workspace_memories",
        "fk_workspace_memories_thread_workspace",
        "agent_threads",
        ("workspace_id", "thread_id"),
        "thread_id",
    ),
    (
        "workspace_memories",
        "fk_workspace_memories_run_workspace",
        "agent_runs",
        ("workspace_id", "source_run_id"),
        "source_run_id",
    ),
)


def _recreate(action: str) -> None:
    for table, name, target, columns, _ in _CONSTRAINTS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name,
            table,
            target,
            list(columns),
            ["workspace_id", "id"],
            ondelete=action.format(column=_column_of(name)),
        )


def _column_of(name: str) -> str:
    for _, constraint, _, _, column in _CONSTRAINTS:
        if constraint == name:
            return column
    raise LookupError(name)


def upgrade() -> None:
    _recreate("SET NULL ({column})")


def downgrade() -> None:
    _recreate("SET NULL")
