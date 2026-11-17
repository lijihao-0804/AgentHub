import subprocess
import sys
from uuid import uuid4

import pytest

from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
    ToolSourceKind,
)
from packages.tools.policy import ToolPolicy, ToolPolicyDecision


@pytest.mark.parametrize(
    "effect,risk,source,sensitive",
    [
        ("READ", "HIGH", "BUILTIN", True),
        ("READ", "HIGH", "MCP", True),
        ("WRITE", "LOW", "MCP", True),
        ("WRITE", "MEDIUM", "BUILTIN", False),
        ("READ", "LOW", "BUILTIN", False),
    ],
)
def test_mandatory_human_review_does_not_depend_on_tool_display_name(
    effect, risk, source, sensitive
):
    definition = ToolDefinition(
        "calculator",
        uuid4(),
        "hash",
        "",
        {},
        ToolEffect(effect),
        ToolRisk(risk),
        ToolApprovalPolicy.NEVER,
        5,
        source_kind=ToolSourceKind(source),
    )
    assert ToolPolicy.requires_human(definition) is sensitive
    if sensitive:
        assert ToolPolicy.decide(definition) is ToolPolicyDecision.REQUIRE_APPROVAL


def test_worker_cold_import_does_not_pull_a_half_initialized_runtime():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import apps.worker.tasks.knowledge; import apps.worker.tasks.approvals; "
            "from packages.approvals import ApprovalService",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )
