"""Verify the Windows-only protected DACL used by the portfolio demonstration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PERMISSIONS_SCRIPT = ROOT / "infra/portfolio-permissions.ps1"
POWERSHELL = shutil.which("pwsh.exe") or shutil.which("powershell.exe")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="Windows ACL verification requires Windows PowerShell.",
)

ACL_PROBE = r"""
$acl = Get-Acl -LiteralPath $env:PORTFOLIO_ACL_PROBE_PATH
$rules = @($acl.Access | ForEach-Object {
    [pscustomobject]@{
        sid = $_.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        rights = $_.FileSystemRights.ToString()
        access_type = $_.AccessControlType.ToString()
        inherited = $_.IsInherited
        inheritance = $_.InheritanceFlags.ToString()
        propagation = $_.PropagationFlags.ToString()
    }
})
[pscustomobject]@{
    protected = $acl.AreAccessRulesProtected
    canonical = $acl.AreAccessRulesCanonical
    sddl = $acl.Sddl
    rules = $rules
} | ConvertTo-Json -Compress -Depth 5
"""


def _run_powershell(command: str, environment: dict[str, str] | None = None):
    return subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **(environment or {})},
    )


def _run_hardener(workspace: Path, state: Path):
    return subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(PERMISSIONS_SCRIPT),
            "-WorkspaceRoot",
            str(workspace),
            "-StatePath",
            str(state),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _acl_snapshot(path: Path) -> dict:
    result = _run_powershell(
        ACL_PROBE,
        {"PORTFOLIO_ACL_PROBE_PATH": str(path)},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _current_sid() -> str:
    result = _run_powershell(
        "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_hardener_preserves_bytes_and_sets_protected_inheriting_dacl(tmp_path):
    workspace = tmp_path / "workspace"
    state = workspace / ".scratch/portfolio-closeout"
    state.mkdir(parents=True)
    private_config = state / "private-config.json"
    compose_env = state / "compose.env"
    private_bytes = b'{"fixture": "not-secret-1"}\n'
    compose_bytes = b"FIXTURE_VALUE=not-secret-2\n"
    private_config.write_bytes(private_bytes)
    compose_env.write_bytes(compose_bytes)
    parent_sddl = _acl_snapshot(state.parent)["sddl"]

    result = _run_hardener(workspace, state)

    assert result.returncode == 0, result.stderr
    assert private_config.read_bytes() == private_bytes
    assert compose_env.read_bytes() == compose_bytes
    assert _acl_snapshot(state.parent)["sddl"] == parent_sddl

    expected_sids = {_current_sid(), "S-1-5-18", "S-1-5-32-544"}
    state_acl = _acl_snapshot(state)
    assert state_acl["protected"] is True
    assert state_acl["canonical"] is True
    assert {rule["sid"] for rule in state_acl["rules"]} == expected_sids
    assert all(rule["rights"] == "FullControl" for rule in state_acl["rules"])
    assert all(rule["access_type"] == "Allow" for rule in state_acl["rules"])
    assert all(rule["inherited"] is False for rule in state_acl["rules"])
    assert all(
        rule["inheritance"] == "ContainerInherit, ObjectInherit"
        for rule in state_acl["rules"]
    )

    for config_path in (private_config, compose_env):
        config_acl = _acl_snapshot(config_path)
        assert config_acl["protected"] is True
        assert {rule["sid"] for rule in config_acl["rules"]} == expected_sids
        assert all(rule["inherited"] is False for rule in config_acl["rules"])
        assert all(rule["inheritance"] == "None" for rule in config_acl["rules"])

    newly_created = state / "later-output.txt"
    newly_created.write_text("fixture\n", encoding="utf-8")
    inherited_acl = _acl_snapshot(newly_created)
    assert {rule["sid"] for rule in inherited_acl["rules"]} == expected_sids
    assert all(rule["inherited"] is True for rule in inherited_acl["rules"])

    # The exact same DACL must be reapplied without audit/owner privileges.
    repeated = _run_hardener(workspace, state)
    assert repeated.returncode == 0, repeated.stderr
    assert private_config.read_bytes() == private_bytes
    assert compose_env.read_bytes() == compose_bytes
    assert _acl_snapshot(state)["sddl"] == state_acl["sddl"]


def test_hardener_rejects_state_tree_reparse_point_before_changing_acls(tmp_path):
    workspace = tmp_path / "workspace"
    state = workspace / ".scratch/portfolio-closeout"
    target = tmp_path / "outside"
    state.mkdir(parents=True)
    target.mkdir()
    (target / "marker.txt").write_text("outside fixture\n", encoding="utf-8")
    junction = state / "linked-outside"
    creation = _run_powershell(
        "New-Item -ItemType Junction -Path $env:PORTFOLIO_JUNCTION_PATH "
        "-Target $env:PORTFOLIO_JUNCTION_TARGET | Out-Null",
        {
            "PORTFOLIO_JUNCTION_PATH": str(junction),
            "PORTFOLIO_JUNCTION_TARGET": str(target),
        },
    )
    assert creation.returncode == 0, creation.stderr

    try:
        state_sddl = _acl_snapshot(state)["sddl"]
        target_sddl = _acl_snapshot(target)["sddl"]
        result = _run_hardener(workspace, state)
        assert result.returncode != 0
        assert "reparse point" in result.stderr.lower()
        assert _acl_snapshot(state)["sddl"] == state_sddl
        assert _acl_snapshot(target)["sddl"] == target_sddl
    finally:
        _run_powershell(
            "Remove-Item -LiteralPath $env:PORTFOLIO_JUNCTION_PATH -Force",
            {"PORTFOLIO_JUNCTION_PATH": str(junction)},
        )
