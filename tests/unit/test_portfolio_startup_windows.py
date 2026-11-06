"""Windows launcher fault injection; fake roles never use the real demo data."""

import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("pwsh.exe") or "powershell.exe"
pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows process/PowerShell launcher"
)


def fixture_checkout(tmp_path, *, worker_exit=False):
    (tmp_path / "infra").mkdir()
    (tmp_path / "scripts").mkdir()
    state = tmp_path / ".scratch/portfolio-closeout"
    state.mkdir(parents=True)
    (state / "private-config.json").write_text("{}")
    shutil.copyfile(ROOT / "infra/portfolio-local.ps1", tmp_path / "infra/portfolio-local.ps1")
    (tmp_path / "scripts/portfolio_demo.py").write_text(
        "import sys,time\n"
        "if sys.argv[1]=='secure-state': sys.exit(0)\n"
        + ("if sys.argv[1]=='worker': sys.exit(37)\n" if worker_exit else "")
        + "time.sleep(60)\n"
    )
    (tmp_path / "scripts/portfolio_readiness.py").write_text("import time; time.sleep(60)\n")
    return state


def launch(tmp_path, timeout_seconds=5):
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-File",
            str(tmp_path / "infra/portfolio-local.ps1"),
            "Start",
            "-Python",
            sys.executable,
            "-ReadyTimeoutSeconds",
            str(timeout_seconds),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=25,
    )


def assert_children_stopped(state):
    records = json.loads((state / "processes.json").read_text(encoding="utf-8-sig"))
    pids = ",".join(str(record["pid"]) for record in records)
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-Command",
            f"@(Get-Process -Id {pids} -ErrorAction SilentlyContinue).Count",
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
    )
    assert result.stdout.strip() == "0"


def test_ops_port_conflict_starts_no_processes_and_preserves_listener(tmp_path):
    state = fixture_checkout(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 8946))
        listener.listen()
        result = launch(tmp_path)
        assert result.returncode != 0
        assert "8946" in result.stderr and "occupied" in result.stderr
        assert not (state / "processes.json").exists()
        assert listener.getsockname()[1] == 8946


def test_worker_early_exit_fails_and_cleans_up_roles(tmp_path):
    state = fixture_checkout(tmp_path, worker_exit=True)
    result = launch(tmp_path, timeout_seconds=15)
    assert result.returncode != 0
    assert "worker" in result.stderr and "exited before readiness" in result.stderr
    assert "logs" in result.stderr
    assert_children_stopped(state)


def test_readiness_timeout_is_nonzero_and_cleans_up_roles(tmp_path):
    state = fixture_checkout(tmp_path)
    result = launch(tmp_path)
    assert result.returncode != 0
    assert "deadline exceeded" in result.stderr and "logs" in result.stderr
    assert_children_stopped(state)
