"""Label a thread with the application surface it belongs to.

Four applications now share one thread table, and a page needs to know which
threads are its own. Deriving that from the agent would be wrong twice over: an
agent can be renamed or re-pointed after the fact, and a workspace may
reasonably run two incident agents or none.

Like ``artifacts.type``, the column carries no CHECK constraint. The allowed
set is an application-layer whitelist in ``packages.threads.kinds``, which
refuses just as hard and does not cost a migration each time an application is
added. Existing rows become ``general``: they predate the distinction, and
calling them research retroactively would be a guess written into the data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Kept within 32 characters: alembic_version.version_num is varchar(32).
revision: str = "0025_thread_kind"
down_revision: str | None = "0024_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_threads",
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'general'"),
        ),
    )
    # Every application page lists its own threads newest first, which is this
    # index and nothing else.
    op.create_index(
        "ix_agent_threads_workspace_kind",
        "agent_threads",
        ["workspace_id", "kind", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_threads_workspace_kind", table_name="agent_threads")
    op.drop_column("agent_threads", "kind")
