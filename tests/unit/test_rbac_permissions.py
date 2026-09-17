import pytest

from packages.control_plane.audit import UnsafeAuditMetadata, assert_safe_metadata
from packages.control_plane.enums import OrganizationRole, WorkspaceRole
from packages.control_plane.rbac import (
    APPROVE_ACTION,
    DEVELOPER_PERMISSIONS,
    ORGANIZATION_ADMIN_PERMISSIONS,
    ORGANIZATION_MANAGE,
    RUN_ACTION,
    VIEWER_PERMISSIONS,
    WORKSPACE_ADMIN,
    WORKSPACE_READ,
    ensure_not_last_owner,
    resolve_permissions,
)
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)


def test_organization_admin_permissions_are_workspace_scoped_without_member_role() -> None:
    permissions = resolve_permissions(OrganizationRole.ADMIN, None)

    assert permissions == ORGANIZATION_ADMIN_PERMISSIONS
    assert {
        ORGANIZATION_MANAGE,
        WORKSPACE_ADMIN,
        WORKSPACE_READ,
        APPROVE_ACTION,
        RUN_ACTION,
        "agent_create",
        "agent_edit",
        "agent_run",
        "knowledge_create",
        "knowledge_edit",
        "knowledge_run",
        "tool_create",
        "tool_edit",
        "tool_run",
    } <= permissions


def test_developer_can_run_but_cannot_approve() -> None:
    permissions = resolve_permissions(OrganizationRole.MEMBER, WorkspaceRole.DEVELOPER)

    assert permissions == DEVELOPER_PERMISSIONS
    assert {WORKSPACE_READ, RUN_ACTION, "agent_create", "tool_run"} <= permissions
    assert WORKSPACE_ADMIN not in permissions
    assert APPROVE_ACTION not in permissions


def test_viewer_is_read_only_and_member_without_workspace_access_has_no_permissions() -> None:
    assert resolve_permissions(OrganizationRole.MEMBER, WorkspaceRole.VIEWER) == VIEWER_PERMISSIONS
    assert resolve_permissions(OrganizationRole.MEMBER, None) == frozenset()


def test_last_owner_cannot_be_removed_or_downgraded() -> None:
    with pytest.raises(AgentHubError, match="last organization owner") as exc_info:
        ensure_not_last_owner(1, OrganizationRole.OWNER, OrganizationRole.ADMIN)

    assert exc_info.value.code == "LAST_OWNER_PROTECTION"
    assert exc_info.value.status_code == 409


def test_owner_change_is_allowed_when_another_owner_exists() -> None:
    ensure_not_last_owner(2, OrganizationRole.OWNER, OrganizationRole.ADMIN)
    ensure_not_last_owner(1, OrganizationRole.ADMIN, OrganizationRole.MEMBER)


def test_organization_admin_context_has_no_workspace_membership_role() -> None:
    principal = PrincipalContext(request_id="req-1", trace_id="trace-1", user_id="user-1")
    organization = OrganizationContext(
        principal=principal,
        organization_id="org-1",
        org_role=OrganizationRole.ADMIN,
    )
    context = WorkspaceExecutionContext(
        organization=organization,
        workspace_id="workspace-1",
        workspace_role=None,
        permissions=resolve_permissions(OrganizationRole.ADMIN, None),
    )

    assert context.workspace_role is None
    assert WORKSPACE_ADMIN in context.permissions


def test_audit_metadata_rejects_secret_like_keys_recursively() -> None:
    with pytest.raises(UnsafeAuditMetadata):
        assert_safe_metadata({"nested": [{"refresh_token": "never-store"}]})
