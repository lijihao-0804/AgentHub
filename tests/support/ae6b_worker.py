"""Test-only Celery worker composition for AE-6b queue acceptance."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from apps.worker.celery_app import celery_app
from apps.worker.tasks import evaluation as evaluation_tasks
from apps.worker.tasks import knowledge as knowledge_tasks
from packages.core.config.settings import DEVELOPMENT_ENVIRONMENTS, get_settings
from tests.support.ae6b_faults import install_configured_fault
from tests.support.ae6b_provider import AUDIT_PATH_ENV, ae6b_model_gateway
from tests.support.knowledge import fake_indexing_components


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


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.hostname.strip() or any(character.isspace() for character in args.hostname):
        parser.error("--hostname must be one non-empty token")
    try:
        _require_test_runtime()
    except RuntimeError as error:
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
