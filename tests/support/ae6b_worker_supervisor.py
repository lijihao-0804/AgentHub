"""Portable supervision for one test-owned AE-6b worker process."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOCAL_JOB_RUNNER = Path.home() / ".codex/tools/job_runner.py"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP


class AE6BWorkerSupervisor:
    """Run and stop exactly one worker, preferring the local durable job runner."""

    def __init__(
        self,
        *,
        worker_argv: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        job_dir: Path,
        env: Mapping[str, str],
    ) -> None:
        if not worker_argv or not all(isinstance(value, str) and value for value in worker_argv):
            raise ValueError("worker_argv must contain non-empty strings")
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.worker_argv = tuple(worker_argv)
        self.cwd = Path(cwd).resolve(strict=True)
        self.timeout_seconds = timeout_seconds
        self.job_dir = Path(job_dir)
        self.env = dict(env)
        self.spec_path = self.job_dir.parent / "worker-spec.json"
        self.status_path = self.job_dir / "status.json"
        self.stdout_path = self.job_dir / "stdout.log"
        self.stderr_path = self.job_dir / "stderr.log"
        self.process: subprocess.Popen | None = None
        self._streams: list[Any] = []
        self._started_monotonic: float | None = None
        self.uses_local_job_runner = LOCAL_JOB_RUNNER.exists()

    def start(self) -> subprocess.Popen:
        if self.process is not None:
            raise RuntimeError("AE6B_WORKER_SUPERVISOR_ALREADY_STARTED")
        if self.job_dir.exists():
            raise FileExistsError(self.job_dir)

        try:
            self.job_dir.parent.mkdir(parents=True, exist_ok=True)
            self.spec_path.write_text(
                json.dumps(
                    {
                        "argv": list(self.worker_argv),
                        "cwd": str(self.cwd),
                        "timeout_seconds": self.timeout_seconds,
                    }
                ),
                encoding="utf-8",
            )
            self._started_monotonic = time.monotonic()
            if self.uses_local_job_runner:
                self.process = self._start_with_job_runner()
            else:
                self.process = self._start_direct()
            return self.process
        except BaseException as error:
            self.force_stop()
            self._write_failure_to_start(error)
            self.close()
            raise

    def _start_with_job_runner(self) -> subprocess.Popen:
        supervisor_log = (self.job_dir.parent / "supervisor.log").open("wb")
        self._streams.append(supervisor_log)
        return subprocess.Popen(
            [
                sys.executable,
                str(LOCAL_JOB_RUNNER),
                "run",
                "--spec",
                str(self.spec_path),
                "--job-dir",
                str(self.job_dir),
            ],
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=supervisor_log,
            stderr=subprocess.STDOUT,
            creationflags=_creation_flags(),
            start_new_session=os.name != "nt",
        )

    def _start_direct(self) -> subprocess.Popen:
        self.job_dir.mkdir(parents=True, exist_ok=False)
        self._write_status(state="starting", pid=None)
        stdout = self.stdout_path.open("wb")
        stderr = self.stderr_path.open("wb")
        self._streams.extend((stdout, stderr))
        self.process = subprocess.Popen(
            self.worker_argv,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=_creation_flags(),
            start_new_session=os.name != "nt",
        )
        self._write_status(state="running", pid=self.process.pid)
        return self.process

    def _write_status(
        self,
        *,
        state: str,
        pid: int | None,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            self.status_path,
            {
                "state": state,
                "started_at": _timestamp(),
                "cwd": str(self.cwd),
                "pid": pid,
                "exit_code": exit_code,
                "stdout": str(self.stdout_path),
                "stderr": str(self.stderr_path),
                "supervisor_pid": os.getpid(),
                **({"error": error} if error is not None else {}),
            },
        )

    def _write_failure_to_start(self, error: BaseException) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        current = self._read_status() or {}
        worker_exit_code = current.get("exit_code")
        supervisor_exit_code = (
            self.process.poll() if self.uses_local_job_runner and self.process is not None else None
        )
        if worker_exit_code is None and current.get("state") in {
            None,
            "dispatched",
            "starting",
        }:
            current["state"] = "failed_to_start"
        current.update(
            {
                "pid": current.get("pid", self.process.pid if self.process is not None else None),
                "exit_code": worker_exit_code,
                "supervisor_exit_code": supervisor_exit_code,
                "worker_exit_code": worker_exit_code,
                "error": f"{type(error).__name__}: {error}",
                "finished_at": current.get("finished_at") or _timestamp(),
            }
        )
        current.setdefault("cwd", str(self.cwd))
        current.setdefault("stdout", str(self.stdout_path))
        current.setdefault("stderr", str(self.stderr_path))
        _write_json(self.status_path, current)

    def _read_status(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _finalize_exit(self, process_exit_code: int) -> dict[str, Any]:
        existing = self._read_status()
        current = existing or {}
        elapsed_seconds = (
            round(time.monotonic() - self._started_monotonic, 3)
            if self._started_monotonic is not None
            else None
        )
        if self.uses_local_job_runner:
            worker_exit_code = current.get("exit_code")
            current.update(
                {
                    "supervisor_exit_code": process_exit_code,
                    "worker_exit_code": worker_exit_code,
                }
            )
            if existing is None:
                current.update(
                    {
                        "state": "failed_to_start",
                        "exit_code": None,
                        "finished_at": _timestamp(),
                        "elapsed_seconds": elapsed_seconds,
                    }
                )
        else:
            current.update(
                {
                    "state": "completed" if process_exit_code == 0 else "failed",
                    "exit_code": process_exit_code,
                    "supervisor_exit_code": None,
                    "worker_exit_code": process_exit_code,
                    "finished_at": _timestamp(),
                    "elapsed_seconds": elapsed_seconds,
                }
            )
        if self.uses_local_job_runner:
            current.setdefault("cwd", str(self.cwd))
            current.setdefault("stdout", str(self.stdout_path))
            current.setdefault("stderr", str(self.stderr_path))
        else:
            current.update(
                {
                    "cwd": str(self.cwd),
                    "stdout": str(self.stdout_path),
                    "stderr": str(self.stderr_path),
                }
            )
        current.setdefault("exit_code", None)
        current.setdefault("finished_at", _timestamp())
        current.setdefault("elapsed_seconds", elapsed_seconds)
        if current.get("pid") is None and self.process is not None:
            current["pid"] = self.process.pid
        _write_json(self.status_path, current)
        return current

    async def wait(self, *, timeout: float | None = None) -> int:
        if self.process is None:
            raise RuntimeError("AE6B_WORKER_SUPERVISOR_NOT_STARTED")
        exit_code = await asyncio.to_thread(self.process.wait, timeout=timeout)
        self._finalize_exit(exit_code)
        return exit_code

    def status(self) -> dict[str, Any]:
        return json.loads(self.status_path.read_text(encoding="utf-8"))

    def status_cwd_matches(self, expected: Path) -> bool:
        raw_cwd = self.status().get("cwd")
        return self._cwd_matches(raw_cwd, expected)

    @staticmethod
    def _cwd_matches(raw_cwd: object, expected: Path) -> bool:
        if not isinstance(raw_cwd, str) or not raw_cwd:
            return False
        actual = Path(raw_cwd).resolve(strict=False)
        target = Path(expected).resolve(strict=False)
        try:
            return actual.samefile(target)
        except OSError:
            return os.path.normcase(str(actual)) == os.path.normcase(str(target))

    def _verified_job_runner_child_pid(self) -> int | None:
        if not self.uses_local_job_runner or self.process is None:
            return None
        current = self._read_status()
        if current is None:
            return None
        child_pid = current.get("pid")
        if (
            isinstance(child_pid, bool)
            or not isinstance(child_pid, int)
            or child_pid <= 0
            or child_pid == self.process.pid
            or not self._cwd_matches(current.get("cwd"), self.cwd)
        ):
            return None
        return child_pid

    def _terminate_tree(self, pid: int, *, isolated_group: bool = False) -> None:
        if os.name == "nt":
            stopped = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if stopped.returncode != 0:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            return

        try:
            child_group = os.getpgid(pid)
            supervisor_group = (
                os.getpgid(self.process.pid) if self.process is not None else child_group
            )
        except ProcessLookupError:
            return
        try:
            if isolated_group or child_group != supervisor_group:
                os.killpg(child_group, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def force_stop(self) -> None:
        """Stop this supervisor's process tree; never target an unrelated worker."""

        if self.process is None:
            return
        if self.process.poll() is not None:
            self._finalize_exit(self.process.returncode)
            return

        if self.uses_local_job_runner:
            child_pid = self._verified_job_runner_child_pid()
            if child_pid is not None:
                # The runner remains alive to observe the real worker exit and
                # publish it. Killing the runner here would conflate its exit
                # code with the worker's code.
                self._terminate_tree(child_pid)
            else:
                # No verifiable worker identity means startup never reached a
                # safely attributable child. Stop the owned supervisor tree.
                self._terminate_tree(self.process.pid, isolated_group=True)
            try:
                supervisor_exit_code = self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._terminate_tree(self.process.pid, isolated_group=True)
                supervisor_exit_code = self.process.wait(timeout=10)
            self._finalize_exit(supervisor_exit_code)
            return

        self._terminate_tree(self.process.pid, isolated_group=True)
        if os.name != "nt":
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
        worker_exit_code = self.process.wait(timeout=10)
        self._finalize_exit(worker_exit_code)

    async def stop(self, *, timeout: float = 15) -> None:
        try:
            if self.process is None:
                return
            if self.process.poll() is None:
                try:
                    await self.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    await asyncio.to_thread(self.force_stop)
            else:
                self._finalize_exit(self.process.returncode)
        finally:
            self.close()

    def close(self) -> None:
        for stream in self._streams:
            if not stream.closed:
                stream.close()
        self._streams.clear()


__all__ = ["AE6BWorkerSupervisor", "LOCAL_JOB_RUNNER"]
