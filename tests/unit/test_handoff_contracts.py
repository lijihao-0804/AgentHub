import pytest
from pydantic import ValidationError

from apps.api.schemas.handoffs import CloseRequest, VersionRequest
from packages.control_plane.rbac import HANDOFF_HANDLE, WORKSPACE_ADMIN, resolve_permissions


@pytest.mark.parametrize("version", [True, False, 0, -1, "1", 1.5])
def test_version_is_strict_positive_integer(version):
    with pytest.raises(ValidationError):
        VersionRequest(expected_version=version)


@pytest.mark.parametrize(
    "payload",
    [
        {"reason": " "},
        {"reason": "x" * 4001},
        {"reason": "ok", "unresolved_items": [" "]},
        {"reason": "ok", "unresolved_items": ["x" * 1001]},
        {"reason": "ok", "unresolved_items": ["x"] * 51},
        {"reason": "ok", "status": "CLOSED"},
    ],
)
def test_close_rejects_invalid_content(payload):
    with pytest.raises(ValidationError):
        CloseRequest(expected_version=1, **payload)


def test_close_normalizes_content_and_keeps_unresolved_items():
    result = CloseRequest(
        expected_version=1, reason=" inspected ", unresolved_items=[" uncertain "]
    )
    assert result.reason == "inspected" and result.unresolved_items == ["uncertain"]


def test_handoff_rbac_assign_is_admin_only():
    assert HANDOFF_HANDLE in resolve_permissions("MEMBER", "DEVELOPER")
    assert WORKSPACE_ADMIN not in resolve_permissions("MEMBER", "DEVELOPER")
    assert HANDOFF_HANDLE not in resolve_permissions("MEMBER", "VIEWER")
    assert HANDOFF_HANDLE in resolve_permissions("OWNER", None)
