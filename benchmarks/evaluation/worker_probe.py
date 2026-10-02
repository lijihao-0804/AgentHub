"""Own a local Celery worker for isolated broker/database integration tests."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

WORKER_DB = "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub_mi34_worker_20261002"
WORKER_REDIS = "redis://localhost:6384/0"


def run(directory: Path, output: Path):
    directory.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    environment.update(
        {
            "AGENTHUB_DATABASE_URL": WORKER_DB,
            "AGENTHUB_TEST_DATABASE_URL": WORKER_DB,
            "AGENTHUB_REDIS_URL": WORKER_REDIS,
            "AGENTHUB_TEST_REDIS_URL": WORKER_REDIS,
            "AGENTHUB_BLOB_ROOT": str((directory / "blobs").resolve()),
            "AGENTHUB_KNOWLEDGE_QDRANT_COLLECTION": "agenthub_mi34_worker_20261002",
            "AGENTHUB_KNOWLEDGE_EMBEDDING_DEVICE": "cpu",
            "AGENTHUB_KNOWLEDGE_RERANKER_DEVICE": "cpu",
            "AGENTHUB_KNOWLEDGE_WARM_MODELS_ON_START": "false",
            "HF_HUB_OFFLINE": "1",
        }
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=environment,
        check=False,
        creationflags=flags,
        timeout=60,
    )
    if migration.returncode:
        raise RuntimeError("ISOLATED_MIGRATION_FAILED")
    code = (
        "from apps.worker.celery_app import celery_app; "
        "from packages.core.config.settings import get_settings; "
        "from packages.knowledge.composition import warm_retrieval_components; "
        "warm_retrieval_components(get_settings()); "
        "celery_app.worker_main(['worker','--pool=solo','--concurrency=1',"
        "'--loglevel=INFO','--hostname=mi34-isolated@%h','--without-gossip','--without-mingle'])"
    )
    log_path = directory / "worker.log"
    with log_path.open("wb") as log:
        worker = subprocess.Popen(
            [sys.executable, "-c", code],
            env=environment,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        try:
            deadline = monotonic() + 180
            while " ready." not in log_path.read_text(encoding="utf-8", errors="replace"):
                if worker.poll() is not None:
                    raise RuntimeError("ISOLATED_WORKER_EXITED_BEFORE_READY")
                if monotonic() >= deadline:
                    raise TimeoutError("ISOLATED_WORKER_START_TIMEOUT")
                sleep(0.5)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/integration/test_m3b_worker.py",
                    "-q",
                    "--basetemp=" + str(directory / "pytest-temp"),
                    "--junitxml=" + str(output.with_suffix(".xml")),
                ],
                env=environment,
                check=False,
                creationflags=flags,
                timeout=180,
            )
        finally:
            worker.terminate()
            worker.wait(timeout=15)
    output.write_text(
        json.dumps(
            {
                "database": "agenthub_mi34_worker_20261002",
                "redis": WORKER_REDIS,
                "worker_pool": "solo",
                "worker_concurrency": 1,
                "provider": "cached_local_bge",
                "HF_HUB_OFFLINE": "1",
                "device": "cpu",
                "worker_pid": worker.pid,
                "worker_stopped": worker.poll() is not None,
                "pytest_exit_code": result.returncode,
                "worker_log": str(log_path),
                "boundary": (
                    "real broker/worker/indexing; not a distributed worker process crash test"
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if result.returncode:
        raise RuntimeError("WORKER_TESTS_FAILED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.directory, args.output)
