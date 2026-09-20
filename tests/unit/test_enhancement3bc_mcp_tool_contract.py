"""Enhancement 3B: the frozen contract a remote tool is imported as.

The point of these tests is that "where the body lives" and "what the body is
allowed to do" stay separate. A remote tool arrives carrying the same
governance fields a builtin one does, passes the same validators, and hashes
the same way; nothing about it being remote gives it a different route through
the system.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from packages.agent_runtime.tool_revisions import validate_tool_spec
from packages.control_plane.product_control_plane import tool_projection
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.mcp.service import _validated_governance
from packages.tools.contracts import ToolSourceKind
from packages.tools.validation import is_valid_tool_identity, validate_executable_tool_spec

CONNECTION_ID = str(uuid4())
INPUT_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def builtin_spec(**overrides: object) -> dict:
    spec = {
        "kind": "builtin",
        "identity": "calculator",
        "description": "Evaluate arithmetic.",
        "input_schema": INPUT_SCHEMA,
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "timeout_seconds": 30,
    }
    spec.update(overrides)
    return spec


def mcp_spec(**overrides: object) -> dict:
    spec = {
        "kind": "mcp",
        "identity": "crm_search_customer",
        "description": "Search the CRM.",
        "input_schema": INPUT_SCHEMA,
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "timeout_seconds": 30,
        "mcp": {
            "connection_id": CONNECTION_ID,
            "tool_name": "search_customer",
            "output_schema": None,
        },
    }
    spec.update(overrides)
    return spec


class _Row:
    def __init__(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_builtin_spec_is_unchanged_by_the_new_kinds() -> None:
    normalized = validate_executable_tool_spec(builtin_spec())

    assert normalized["kind"] == "builtin"
    assert "mcp" not in normalized
    assert normalized == builtin_spec()


def test_builtin_spec_may_not_smuggle_remote_wiring() -> None:
    with pytest.raises(AgentHubError):
        validate_executable_tool_spec(
            builtin_spec(mcp={"connection_id": CONNECTION_ID, "tool_name": "x"})
        )


def test_mcp_spec_validates_and_normalizes_its_remote_block() -> None:
    normalized = validate_executable_tool_spec(mcp_spec())

    assert normalized["mcp"] == {
        "connection_id": CONNECTION_ID,
        "tool_name": "search_customer",
        "output_schema": None,
    }


@pytest.mark.parametrize(
    "block",
    [
        {"tool_name": "search_customer"},
        {"connection_id": CONNECTION_ID},
        {"connection_id": "not-a-uuid", "tool_name": "search_customer"},
        {"connection_id": CONNECTION_ID, "tool_name": ""},
        {"connection_id": CONNECTION_ID, "tool_name": "x" * 129},
        {"connection_id": CONNECTION_ID, "tool_name": "x", "endpoint_url": "https://a/mcp"},
        {"connection_id": CONNECTION_ID, "tool_name": "x", "output_schema": {"type": 7}},
        "not-an-object",
    ],
)
def test_invalid_remote_block_is_rejected(block) -> None:
    with pytest.raises(AgentHubError):
        validate_executable_tool_spec(mcp_spec(mcp=block))


def test_remote_output_schema_must_be_a_real_json_schema() -> None:
    normalized = validate_executable_tool_spec(
        mcp_spec(
            mcp={
                "connection_id": CONNECTION_ID,
                "tool_name": "search_customer",
                "output_schema": {"type": "object"},
            }
        )
    )

    assert normalized["mcp"]["output_schema"] == {"type": "object"}


def test_secret_shaped_keys_are_rejected_inside_a_remote_spec() -> None:
    # The credential belongs to the connection. A revision is read by anyone who
    # can read the workspace's tools, so it must never be able to carry one.
    with pytest.raises(AgentHubError):
        validate_tool_spec(
            mcp_spec(
                mcp={
                    "connection_id": CONNECTION_ID,
                    "tool_name": "search_customer",
                    "authorization": "Bearer hunter2",
                }
            )
        )


@pytest.mark.parametrize(
    "identity",
    ["crm_search", "Create-Order", "a", "x" * 64],
)
def test_accepted_identities(identity: str) -> None:
    assert is_valid_tool_identity(identity)


@pytest.mark.parametrize(
    "identity",
    ["", "x" * 65, "has space", "has.dot", "emoji🙂", "tool/name", None, 7],
)
def test_rejected_identities_are_never_rewritten(identity) -> None:
    assert not is_valid_tool_identity(identity)


def test_write_may_not_be_imported_without_approval() -> None:
    with pytest.raises(AgentHubError) as excinfo:
        _validated_governance(
            effect="WRITE", risk_level="HIGH", approval_policy="NEVER", timeout_seconds=None
        )

    assert excinfo.value.code == "MCP_TOOL_GOVERNANCE_INVALID"


def test_high_risk_read_also_requires_approval() -> None:
    with pytest.raises(AgentHubError):
        _validated_governance(
            effect="READ", risk_level="HIGH", approval_policy="NEVER", timeout_seconds=None
        )


def test_read_may_be_imported_as_unattended() -> None:
    governance = _validated_governance(
        effect="READ", risk_level="LOW", approval_policy="NEVER", timeout_seconds=None
    )

    assert governance == {
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "timeout_seconds": 30,
    }


@pytest.mark.parametrize(
    ("effect", "risk_level", "approval_policy"),
    [("MUTATE", "LOW", "NEVER"), ("READ", "EXTREME", "NEVER"), ("READ", "LOW", "SOMETIMES")],
)
def test_governance_values_must_be_known(effect, risk_level, approval_policy) -> None:
    with pytest.raises(AgentHubError):
        _validated_governance(
            effect=effect,
            risk_level=risk_level,
            approval_policy=approval_policy,
            timeout_seconds=None,
        )


def test_spec_hash_is_stable_across_key_order() -> None:
    shuffled = dict(reversed(list(mcp_spec().items())))

    assert canonical_json_hash(validate_executable_tool_spec(shuffled)) == canonical_json_hash(
        validate_executable_tool_spec(mcp_spec())
    )


def test_spec_hash_changes_when_the_remote_tool_name_changes() -> None:
    other = mcp_spec(
        mcp={
            "connection_id": CONNECTION_ID,
            "tool_name": "search_customer_v2",
            "output_schema": None,
        }
    )

    assert canonical_json_hash(validate_executable_tool_spec(other)) != canonical_json_hash(
        validate_executable_tool_spec(mcp_spec())
    )


def test_source_kind_defaults_to_builtin() -> None:
    assert ToolSourceKind.BUILTIN.value == "BUILTIN"
    assert ToolSourceKind.MCP.value == "MCP"


@pytest.mark.parametrize(
    ("spec", "expected_execution_kind", "expected_source_kind"),
    [
        (builtin_spec(), "builtin", "builtin"),
        (builtin_spec(identity="create_ticket", effect="WRITE"), "action", "builtin"),
        (mcp_spec(), "mcp", "mcp"),
        (
            mcp_spec(effect="WRITE", risk_level="HIGH", approval_policy="ALWAYS"),
            "action",
            "mcp",
        ),
    ],
)
def test_tool_projection_labels_source_and_execution_separately(
    spec, expected_execution_kind, expected_source_kind
) -> None:
    # execution_kind is a label for the management UI. Nothing in the runtime
    # reads it, which is why a remote WRITE is shown as an action: that is what
    # actually decides how it runs.
    tool = _Row(
        id=uuid4(),
        workspace_id=uuid4(),
        name="Tool",
        description=None,
        enabled=True,
        created_at=None,
    )
    revision = _Row(
        id=uuid4(), revision_number=1, spec=spec, spec_hash=canonical_json_hash(spec)
    )

    projection = tool_projection(tool, revision)

    assert projection["execution_kind"] == expected_execution_kind
    assert projection["source_kind"] == expected_source_kind
    assert projection["identity"] == spec["identity"]


def test_tool_projection_does_not_require_a_builtin_catalog_entry_for_remote_tools() -> None:
    spec = mcp_spec(identity="nothing_this_server_ships")
    tool = _Row(
        id=uuid4(),
        workspace_id=uuid4(),
        name="Tool",
        description=None,
        enabled=True,
        created_at=None,
    )
    revision = _Row(
        id=uuid4(), revision_number=1, spec=spec, spec_hash=canonical_json_hash(spec)
    )

    assert tool_projection(tool, revision)["source_kind"] == "mcp"
