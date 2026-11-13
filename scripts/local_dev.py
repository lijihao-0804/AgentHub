"""Run the ordinary Windows development processes; Ctrl+C stops this invocation."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import httpx


def worker_ready(name: str) -> bool:
    from apps.worker.celery_app import celery_app

    celery_app.conf.broker_transport_options = {"socket_connect_timeout": 1, "socket_timeout": 1}
    replies = celery_app.control.ping(destination=[name], timeout=1)
    return any(reply.get(name, {}).get("ok") == "pong" for reply in replies)


def ensure_running(processes: dict[str, subprocess.Popen]) -> None:
    for role, process in processes.items():
        status = process.poll()
        if status is not None:
            raise RuntimeError(f"{role} exited with status {status}.")


def wait_ready(processes: dict[str, subprocess.Popen], worker: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    checks = dict.fromkeys(("API dependencies", "Web login", "Web API proxy", "worker"), False)
    with httpx.Client(trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline:
            ensure_running(processes)
            try:
                deps = client.get("http://127.0.0.1:8000/api/v1/dependencies").raise_for_status()
                statuses = deps.json()["dependencies"]
                checks["API dependencies"] = all(
                    statuses.get(key) == "healthy" for key in ("postgres", "redis", "qdrant")
                )
                checks["Web login"] = client.get("http://127.0.0.1:3000/login").is_success
                proxy = client.get("http://127.0.0.1:3000/api/v1/health").raise_for_status()
                checks["Web API proxy"] = proxy.json().get("service") == "agenthub-api"
            except (httpx.HTTPError, KeyError, ValueError):
                checks["Web API proxy"] = False
            if not checks["worker"] and time.monotonic() < deadline:
                # Bound Celery broker retries as well as the time spent waiting for a reply.
                try:
                    probe = subprocess.run(
                        [sys.executable, "-m", "scripts.local_dev", "--ping", worker],
                        capture_output=True,
                        timeout=min(5, max(0.1, deadline - time.monotonic())),
                    )
                    checks["worker"] = probe.returncode == 0
                except subprocess.TimeoutExpired:
                    pass
            if all(checks.values()):
                ensure_running(processes)
                return
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    missing = ", ".join(role for role, ready in checks.items() if not ready)
    raise RuntimeError(f"Startup timed out waiting for {missing}.")


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            result = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode and process.poll() is None:
                raise RuntimeError(f"Could not stop local process {process.pid}.")
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Stopping local process {process.pid} timed out.") from exc


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--ping":
        return 0 if worker_ready(sys.argv[2]) else 1
    if os.name != "nt":
        raise SystemExit("Use Docker Compose on this platform; this launcher is for Windows.")
    root = Path.cwd()
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js must be on PATH.")
    logs = root / ".scratch" / "local-dev" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    logs.mkdir(parents=True)
    print(f"AgentHub logs: {logs}", flush=True)
    worker = f"agenthub-dev-{os.getpid()}@{socket.gethostname()}"
    celery = [sys.executable, "-m", "celery", "-A", "apps.worker.celery_app"]
    commands = {
        "api": [
            sys.executable,
            "-m",
            "uvicorn",
            "apps.api.main:app",
            "--reload",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        "worker": [
            *celery,
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "--loglevel=INFO",
            "--hostname",
            worker,
        ],
        "beat": [*celery, "beat", "--loglevel=INFO"],
        "web": [
            node,
            "node_modules/next/dist/bin/next",
            "dev",
            "--hostname",
            "127.0.0.1",
            "--port",
            "3000",
        ],
    }
    processes: dict[str, subprocess.Popen] = {}
    try:
        for port in (8000, 3000):
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", port))
        with ExitStack() as stack:
            for role, args in commands.items():
                env = os.environ.copy()
                if role == "web":
                    env["AGENTHUB_API_PROXY_TARGET"] = "http://127.0.0.1:8000"
                else:
                    env["AGENTHUB_PROCESS_ROLE"] = role
                output = stack.enter_context((logs / f"{role}.log").open("wb"))
                process = subprocess.Popen(
                    args,
                    cwd=root / "apps/web" if role == "web" else root,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                processes[role] = process
                stack.callback(stop_process, process)
            wait_ready(processes, worker)
            print("AgentHub is ready at http://127.0.0.1:3000. Press Ctrl+C to stop.", flush=True)
            webbrowser.open("http://127.0.0.1:3000")
            while True:
                ensure_running(processes)
                time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped local application processes. Compose data services are retained.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"Startup or runtime failed: {exc} Logs: {logs}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
