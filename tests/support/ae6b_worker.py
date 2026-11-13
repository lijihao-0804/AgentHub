"""Test-only Celery worker composition for AE-6b queue acceptance."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path

from apps.worker.celery_app import celery_app
from apps.worker.tasks import evaluation as evaluation_tasks
from apps.worker.tasks import knowledge as knowledge_tasks
from packages.core.config.settings import DEVELOPMENT_ENVIRONMENTS, get_settings
from tests.support.ae6b_faults import install_configured_fault
from tests.support.ae6b_provider import AUDIT_PATH_ENV, ae6b_model_gateway
from tests.support.knowledge import fake_indexing_components

ROOT = Path(__file__).resolve().parents[2]
WORKER_IDENTITY_PATH_ENV = "AGENTHUB_AE6B_WORKER_IDENTITY_PATH"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the bounded AE-6b test worker.")
    parser.add_argument(
        "--hostname",
        required=True,
        help="Unique Celery hostname for this worker process.",
    )
    return parser


def _install_test_composition() -> None:
    """Patch only the explicitly composed test-worker provider and index factories."""

    # The production worker owns this factory and uses it for both Runtime and
    # Judge. Replacing the factory preserves the real gateway/runtime path.
    evaluation_tasks._production_model_gateway = ae6b_model_gateway
    knowledge_tasks._production_indexing_components = fake_indexing_components
    install_configured_fault()


def _require_test_runtime() -> None:
    settings = get_settings()
    if not settings.testing and settings.environment.lower() not in DEVELOPMENT_ENVIRONMENTS:
        raise RuntimeError("AE6B_WORKER_REQUIRES_TEST_ENVIRONMENT")
    if not (os.environ.get(AUDIT_PATH_ENV) or "").strip():
        raise RuntimeError(f"AE6B_WORKER_REQUIRES_{AUDIT_PATH_ENV}")


def _write_worker_identity(hostname: str) -> None:
    configured = os.environ.get(WORKER_IDENTITY_PATH_ENV, "").strip()
    if not configured:
        raise RuntimeError(f"AE6B_WORKER_REQUIRES_{WORKER_IDENTITY_PATH_ENV}")
    identity_path = Path(configured).resolve(strict=False)
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = identity_path.with_name(identity_path.name + ".tmp")
    payload = {
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "cwd": str(ROOT),
        "hostname": hostname,
    }
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, identity_path)
    if os.name != "nt":
        try:
            directory_fd = os.open(identity_path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.hostname.strip() or any(character.isspace() for character in args.hostname):
        parser.error("--hostname must be one non-empty token")
    try:
        _require_test_runtime()
    except RuntimeError as error:
        parser.error(str(error))
    try:
        _write_worker_identity(args.hostname)
    except (OSError, RuntimeError) as error:
        parser.error(str(error))
    _install_test_composition()
    celery_app.worker_main(
        [
            "worker",
            "--loglevel=WARNING",
            "--pool=solo",
            "--concurrency=1",
            f"--hostname={args.hostname}",
        ]
    )


if __name__ == "__main__":
    main()
