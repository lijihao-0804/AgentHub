import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values

from scripts import local_dev, local_setup

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fresh_directory(tmp_path, monkeypatch):
    for name in (local_setup.JWT, local_setup.MASTER_KEY):
        monkeypatch.delenv(name, raising=False)
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env.example")
    return tmp_path


def test_fresh_keys_work_and_repeat_or_parallel_init_preserves_the_entire_file(fresh_directory):
    path = fresh_directory / ".env"
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(local_setup.initialize_env, [fresh_directory] * 2))
    values = dotenv_values(path)
    signing_key, encryption_key = values[local_setup.JWT], values[local_setup.MASTER_KEY]
    assert len(signing_key) >= 32 and signing_key != encryption_key
    assert (
        Fernet(encryption_key).decrypt(Fernet(encryption_key).encrypt(b"credential"))
        == b"credential"
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write("# personal settings\nAGENTHUB_LOG_LEVEL=DEBUG\n")
    previous = path.read_bytes()
    local_setup.initialize_env(fresh_directory)
    assert path.read_bytes() == previous

    other_dir = fresh_directory / "other"
    other_dir.mkdir()
    shutil.copyfile(fresh_directory / ".env.example", other_dir / ".env.example")
    local_setup.initialize_env(other_dir)
    other = dotenv_values(other_dir / ".env")
    assert other[local_setup.JWT] != signing_key
    assert other[local_setup.MASTER_KEY] != encryption_key


@pytest.mark.parametrize(
    ("jwt", "master", "error"),
    [
        (local_setup.EXAMPLE_AUTH_JWT_SECRET, "secret-value", local_setup.JWT),
        ("valid-signing-key" * 3, "", local_setup.MASTER_KEY),
    ],
)
def test_incomplete_existing_env_is_never_rekeyed(fresh_directory, jwt, master, error):
    path = fresh_directory / ".env"
    previous = f"# prior deployment\r\n{local_setup.JWT}={jwt}\r\n{local_setup.MASTER_KEY}={master}"
    path.write_bytes(previous.encode())
    with pytest.raises(ValueError, match=error):
        local_setup.initialize_env(fresh_directory)
    assert path.read_bytes() == previous.encode()


def test_external_master_key_is_never_copied_or_silently_changed(fresh_directory, monkeypatch):
    external = Fernet.generate_key().decode()
    monkeypatch.setenv(local_setup.MASTER_KEY, external)
    local_setup.initialize_env(fresh_directory)
    path = fresh_directory / ".env"
    assert external not in path.read_text(encoding="utf-8")
    monkeypatch.delenv(local_setup.MASTER_KEY)
    with pytest.raises(ValueError, match=local_setup.MASTER_KEY):
        local_setup.initialize_env(fresh_directory)


def test_master_key_override_cannot_silently_replace_the_stored_key(fresh_directory, monkeypatch):
    local_setup.initialize_env(fresh_directory)
    path = fresh_directory / ".env"
    original = path.read_bytes()
    monkeypatch.setenv(local_setup.MASTER_KEY, Fernet.generate_key().decode())
    with pytest.raises(ValueError, match="differs between"):
        local_setup.initialize_env(fresh_directory)
    assert path.read_bytes() == original


def test_env_without_any_master_key_entry_keeps_its_original_bytes(fresh_directory):
    path = fresh_directory / ".env"
    previous = (
        f"{local_setup.JWT}={'stored-signing-key' * 3}\r\n"
        "# Keep this database and its provider credentials\r\n"
        "AGENTHUB_LOG_LEVEL=WARNING\r\n"
    ).encode()
    path.write_bytes(previous)
    with pytest.raises(ValueError, match="Restore the key used with this database"):
        local_setup.initialize_env(fresh_directory)
    assert path.read_bytes() == previous


def test_schema_preparation_uses_configured_database_after_successful_upgrade(monkeypatch):
    url = "postgresql+asyncpg://testuser@127.0.0.1:35432/isolated_example"
    monkeypatch.setenv("AGENTHUB_DATABASE_URL", url)
    events = []
    monkeypatch.setattr(local_setup.command, "upgrade", lambda _, rev: events.append(rev))
    monkeypatch.setattr(local_setup, "bootstrap", lambda target: events.append(target))
    local_setup.prepare_database()
    assert events == ["head", url]

    def fail_upgrade(*_args):
        raise RuntimeError("upgrade failed")

    monkeypatch.setattr(local_setup.command, "upgrade", fail_upgrade)
    with pytest.raises(RuntimeError, match="upgrade failed"):
        local_setup.prepare_database()
    assert events == ["head", url]


def test_fresh_env_resolves_real_compose_config_and_gates_serving_processes(fresh_directory):
    if not shutil.which("docker"):
        pytest.skip("requires Docker Compose CLI; no daemon is needed")
    local_setup.initialize_env(fresh_directory)
    cmd = [
        "docker",
        "compose",
        "--env-file",
        str(fresh_directory / ".env"),
        "-f",
        str(ROOT / "docker-compose.yml"),
        "config",
        "--format",
        "json",
    ]
    config = json.loads(subprocess.check_output(cmd, cwd=fresh_directory))
    services = config["services"]
    deploy = services["prepare"]
    assert deploy["command"][-2:] == ["scripts.local_setup", "prepare"]
    for role in ("api", "worker", "beat"):
        assert (
            services[role]["depends_on"]["prepare"]["condition"] == "service_completed_successfully"
        )
        assert (
            services[role]["environment"]["AGENTHUB_DATABASE_URL"]
            == deploy["environment"]["AGENTHUB_DATABASE_URL"]
        )
    assert (
        services["api"]["environment"][local_setup.MASTER_KEY]
        == services["worker"]["environment"][local_setup.MASTER_KEY]
    )
    for details in services.values():
        assert all(port["host_ip"] == "127.0.0.1" for port in details.get("ports", []))


def test_local_supervisor_fails_on_exited_role_and_readiness_deadline():
    with pytest.raises(RuntimeError, match="worker exited with status 3"):
        local_dev.ensure_running({"worker": SimpleNamespace(poll=lambda: 3)})
    with pytest.raises(RuntimeError, match="timed out"):
        local_dev.wait_ready({}, "no-worker", timeout=0)


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        ("", None),
        ("qdrant", "API dependencies"),
        ("login", "Web login"),
        ("proxy", "Web API proxy"),
        ("pong", "worker"),
    ],
)
def test_local_readiness_requires_storage_proxy_login_and_own_worker(monkeypatch, broken, expected):
    def respond(request):
        path = request.url.path
        if path == "/api/v1/dependencies":
            return httpx.Response(
                200,
                json={
                    "dependencies": {
                        "postgres": "healthy",
                        "redis": "healthy",
                        "qdrant": "degraded" if broken == "qdrant" else "healthy",
                    }
                },
            )
        if path == "/login":
            return httpx.Response(503 if broken == "login" else 200)
        assert path == "/api/v1/health"
        return httpx.Response(
            200, json={"service": "other" if broken == "proxy" else "agenthub-api"}
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(local_dev.httpx, "Client", lambda **_: client)

    def worker_probe(args, **_kwargs):
        assert args[-2:] == ["--ping", "owned-test-worker"]
        return SimpleNamespace(returncode=int(broken == "pong"))

    monkeypatch.setattr(local_dev.subprocess, "run", worker_probe)
    if expected:
        with pytest.raises(RuntimeError, match=expected):
            local_dev.wait_ready({}, "owned-test-worker", timeout=0.05)
    else:
        local_dev.wait_ready({}, "owned-test-worker", timeout=0.05)


@pytest.mark.skipif(os.name != "nt", reason="exercises cmd.exe batch control flow")
def test_windows_launchers_propagate_failures_before_traffic(fresh_directory, monkeypatch):
    # Use cmd.exe's current-directory tool lookup in an isolated checkout. No
    # Docker daemon, host service, or real npm/uv executable is invoked.
    directory = fresh_directory
    for name in ("setup-agenthub.bat", "start-agenthub.bat"):
        shutil.copyfile(ROOT / name, directory / name)
    for name in ("uv", "docker", "npm"):
        (directory / f"{name}.cmd").write_text(
            f"@echo {name} %*>>steps.log\n@exit /b 0\n", encoding="ascii"
        )
    python = directory / ".venv/Scripts"
    python.mkdir(parents=True)
    # The batch scripts explicitly select the virtual environment executable.
    shutil.copyfile(sys._base_executable, python / "python.exe")
    monkeypatch.setenv("PYTHONHOME", sys.base_prefix)
    (directory / "scripts").mkdir()
    (directory / "scripts/local_setup.py").write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "with Path('steps.log').open('a') as out: print(sys.argv[1], file=out)\n"
        "sys.exit(9 if sys.argv[1] == os.environ.get('FAIL_STEP') else 0)\n"
    )
    (directory / "scripts/local_dev.py").write_text("raise RuntimeError('launcher was reached')\n")
    frontend = directory / "apps/web/node_modules/next/dist/bin"
    frontend.mkdir(parents=True)
    (frontend / "next").touch()
    monkeypatch.setenv("FAIL_STEP", "prepare")
    for script in ("setup-agenthub.bat", "start-agenthub.bat"):
        result = subprocess.run(["cmd.exe", "/d", "/c", script], cwd=directory, capture_output=True)
        assert result.returncode != 0
        log = (directory / "steps.log").read_text()
        assert "prepare" in log
        assert "launcher was reached" not in result.stderr.decode(errors="replace")
        if script == "setup-agenthub.bat":
            assert log.index("npm --prefix") < log.index("prepare")
        (directory / "steps.log").unlink()


@pytest.mark.skipif(os.name != "nt", reason="exercises Windows child process tree cleanup")
def test_ordinary_shutdown_stops_its_child_tree_and_retains_other_process():
    import _winapi

    sleeper = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess, sys\n"
        f"child = subprocess.Popen([sys.executable, '-c', {sleeper!r}])\n"
        "print(child.pid, flush=True)\n"
        "child.wait()\n"
    )
    other = subprocess.Popen([sys.executable, "-c", sleeper])
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True
    )
    try:
        child_pid = int(parent.stdout.readline())
        # Keep the original process handle, so PID reuse cannot affect the assertion.
        child = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, child_pid)
        try:
            local_dev.stop_process(parent)
            assert parent.poll() is not None
            assert _winapi.WaitForSingleObject(child, 3000) == _winapi.WAIT_OBJECT_0
            assert other.poll() is None
        finally:
            _winapi.CloseHandle(child)
    finally:
        local_dev.stop_process(parent)
        parent.stdout.close()
        other.terminate()
        other.wait(timeout=5)
