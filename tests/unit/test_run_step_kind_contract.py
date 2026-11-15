"""Keep the durable check aligned with every literal runtime step writer."""

import ast
import importlib
import re
from pathlib import Path

from sqlalchemy import CheckConstraint

from packages.agent_runtime.models import RunStep


def test_runtime_step_writers_and_migration_match_orm_admission():
    check = next(
        constraint
        for constraint in RunStep.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == "ck_run_steps_kind"
    )
    allowed = set(re.findall(r"'([A-Z_]+)'", str(check.sqltext)))
    migration = importlib.import_module("migrations.versions.0056_auxiliary_run_steps")
    assert allowed == set(re.findall(r"'([A-Z_]+)'", migration._KINDS))
    emitted = set()
    for path in Path("packages/agent_runtime").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            value = None
            if node.func.attr == "step" and node.args:
                value = node.args[0]
            elif node.func.attr == "_append_step":
                value = next((item.value for item in node.keywords if item.arg == "kind"), None)
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                emitted.add(value.value)
    assert {"EVIDENCE_SELECTION", "AUXILIARY_MODEL"} <= emitted
    assert emitted <= allowed
