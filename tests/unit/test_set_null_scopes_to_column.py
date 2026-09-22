"""A composite SET NULL must name the column it is allowed to null.

Every composite foreign key in this schema leads with ``workspace_id`` so a row
can never point at a parent in another tenant. ``workspace_id`` is also NOT NULL
everywhere. A bare ``ON DELETE SET NULL`` nulls the whole constraint, so the two
rules collide: deleting the parent raises NotNullViolation instead of clearing
the child's reference. ``DELETE /threads/{id}`` returned 500 for every thread
that had ever been run until 0029 scoped these to the provenance column.

This sweeps the metadata rather than naming the four constraints known to have
been wrong, because the next one to be written wrongly is the one worth
catching.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint

# Imported for the side effect of registering every table on the shared
# metadata -- the sweep below is only as complete as what has been imported, so
# this list mirrors migrations/env.py.
from packages.agent_runtime import models as _agent_runtime_models  # noqa: F401
from packages.approvals import models as _approval_models  # noqa: F401
from packages.artifacts import models as _artifact_models  # noqa: F401
from packages.control_plane import models as _control_plane_models  # noqa: F401
from packages.core.database import Base
from packages.evaluation import models as _evaluation_models  # noqa: F401
from packages.knowledge import models as _knowledge_models  # noqa: F401
from packages.mcp import models as _mcp_models  # noqa: F401
from packages.memory import models as _memory_models  # noqa: F401
from packages.model_gateway import models as _model_gateway_models  # noqa: F401
from packages.threads import models as _thread_models  # noqa: F401
from packages.tools import models as _tool_models  # noqa: F401


def _composite_set_null() -> list[tuple[str, ForeignKeyConstraint]]:
    found = []
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            action = (constraint.ondelete or "").strip().upper()
            if action.startswith("SET NULL") and len(constraint.columns) > 1:
                found.append((table.name, constraint))
    return found


def test_every_composite_set_null_names_the_column_it_may_null() -> None:
    offenders = [
        f"{table}.{constraint.name}"
        for table, constraint in _composite_set_null()
        if (constraint.ondelete or "").strip().upper() == "SET NULL"
    ]
    assert not offenders, (
        "these would null the whole key, workspace_id included: " + ", ".join(sorted(offenders))
    )


def test_the_named_column_is_nullable_and_is_not_the_tenant() -> None:
    for table, constraint in _composite_set_null():
        action = (constraint.ondelete or "").strip()
        named = action[action.index("(") + 1 : action.index(")")].strip()
        column = constraint.table.columns[named]
        assert column.nullable, f"{table}.{constraint.name} nulls a NOT NULL column"
        assert named != "workspace_id", f"{table}.{constraint.name} would move the row's tenant"
