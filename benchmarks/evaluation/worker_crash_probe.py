"""Real solo worker crash after Qdrant upsert, recovered by real periodic beat.

The supervisor uses a fresh database and collection and the task's private Redis.
Deterministic indexing is safely repeatable; this is not a customer write retry.
"""

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID, uuid4

from benchmarks.evaluation.artifacts import write_json_atomic


def child(directory, crash):
    from apps.worker.celery_app import celery_app
    from apps.worker.tasks import knowledge
    from packages.core.config.settings import get_settings
    from packages.knowledge.composition import warm_retrieval_components

    if crash:

        async def crash_after_upsert(*args, **kwargs):
            write_json_atomic(
                directory / "crash-marker.json",
                {
                    "job_id": str(kwargs["job_id"]),
                    "window": "after_qdrant_upsert_before_ready_commit",
                    "at": datetime.now(UTC).isoformat(),
                    "exit_code": 73,
                },
            )
            os._exit(73)

        knowledge.finalize_ingestion_ready = crash_after_upsert
    warm_retrieval_components(get_settings())
    celery_app.worker_main(
        [
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "--loglevel=INFO",
            "--hostname=mi34-crash@%h",
            "--without-gossip",
            "--without-mingle",
        ]
    )


async def create_database(name):
    import asyncpg

    connection = await asyncpg.connect("postgresql://agenthub:agenthub@localhost:5432/postgres")
    try:
        # Generated solely from a fixed prefix and UUID hex, never user SQL.
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()


async def seed(database, root):
    from packages.core.database import create_database
    from tests.integration.test_m3b_worker import _create_pending_job

    engine, factory = create_database(database)
    try:
        job_id, revision_id = await _create_pending_job(factory, root)
        return str(job_id), str(revision_id)
    finally:
        await engine.dispose()


async def inspect_job(database, job_id, revision_id):
    from sqlalchemy import select

    from packages.core.database import create_database
    from packages.knowledge.models import DocumentChunk, DocumentRevision, IngestionJob

    engine, factory = create_database(database)
    try:
        async with factory() as session:
            job = await session.get(IngestionJob, UUID(job_id))
            revision = await session.get(DocumentRevision, UUID(revision_id))
            chunks = list(
                await session.scalars(
                    select(DocumentChunk.chunk_id).where(
                        DocumentChunk.document_revision_id == UUID(revision_id)
                    )
                )
            )
            return {
                "status": job.status,
                "revision_status": revision.ingestion_status,
                "stage": job.stage,
                "attempt_count": job.attempt_count,
                "lease_expires_at": job.lease_expires_at.isoformat()
                if job.lease_expires_at
                else None,
                "chunk_ids": sorted(chunks),
            }
    finally:
        await engine.dispose()


def wait_ready(process, log, timeout=180):
    deadline = monotonic() + timeout
    while " ready." not in log.read_text(encoding="utf-8", errors="replace"):
        if process.poll() is not None:
            raise RuntimeError("PROBE_WORKER_EXITED_BEFORE_READY")
        if monotonic() >= deadline:
            raise TimeoutError("PROBE_WORKER_READY_TIMEOUT")
        sleep(0.5)


def run(directory, output):
    directory.mkdir(parents=True, exist_ok=False)
    name = f"agenthub_mi34_crash_{uuid4().hex[:12]}"
    database = f"postgresql+asyncpg://agenthub:agenthub@localhost:5432/{name}"
    collection = name
    asyncio.run(create_database(name))
    environment = dict(os.environ)
    environment.update(
        {
            "AGENTHUB_DATABASE_URL": database,
            "AGENTHUB_REDIS_URL": "redis://localhost:6384/1",
            "AGENTHUB_BLOB_ROOT": str((directory / "blobs").resolve()),
            "AGENTHUB_KNOWLEDGE_QDRANT_COLLECTION": collection,
            "AGENTHUB_KNOWLEDGE_EMBEDDING_DEVICE": "cpu",
            "AGENTHUB_KNOWLEDGE_RERANKER_DEVICE": "cpu",
            "AGENTHUB_KNOWLEDGE_WARM_MODELS_ON_START": "false",
            "AGENTHUB_KNOWLEDGE_INGESTION_LEASE_SECONDS": "10",
            "AGENTHUB_KNOWLEDGE_INGESTION_LEASE_RENEWAL_SECONDS": "2",
            "AGENTHUB_KNOWLEDGE_INGESTION_ENQUEUE_GRACE_SECONDS": "0",
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_CACHE": "E:/JAVA/AI+agent/hf_cache",
        }
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=environment,
        creationflags=flags,
        timeout=60,
    )
    if migration.returncode:
        raise RuntimeError("PROBE_MIGRATION_FAILED")
    job_id, revision_id = asyncio.run(seed(database, directory / "blobs"))
    report = {
        "status": "RUNNING",
        "database": name,
        "collection": collection,
        "job_id": job_id,
        "revision_id": revision_id,
        "provider": "cached_local_bge_cpu",
        "lease_seconds": 10,
        "periodic_reconciliation_seconds": 60,
        "limitations": [
            "solo_worker_not_multi_host_partition",
            "repeatable_indexing_not_external_business_write",
        ],
    }
    processes, handles = [], []
    try:

        def worker(crash, label):
            log = directory / f"{label}.log"
            handle = log.open("wb")
            handles.append(handle)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.evaluation.worker_crash_probe",
                    "--child",
                    "crash" if crash else "recover",
                    "--directory",
                    str(directory),
                ],
                env=environment,
                stdout=handle,
                stderr=handle,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
            processes.append(process)
            wait_ready(process, log)
            return process

        first = worker(True, "worker-crash")
        from apps.worker.celery_app import create_celery_app
        from packages.core.config.settings import Settings

        app = create_celery_app(
            Settings(database_url=database, redis_url="redis://localhost:6384/1")
        )
        started = monotonic()
        app.send_task("agenthub.process_knowledge_ingestion", args=[job_id])
        exit_code = first.wait(timeout=90)
        report["crashed_worker_exit_code"] = exit_code
        if exit_code != 73 or not (directory / "crash-marker.json").exists():
            raise RuntimeError("PROBE_CRASH_WINDOW_NOT_REACHED")
        report["before_recovery"] = asyncio.run(inspect_job(database, job_id, revision_id))
        restart = monotonic()
        recovered = worker(False, "worker-recover")
        beat_log = (directory / "beat.log").open("wb")
        handles.append(beat_log)
        beat = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from apps.worker.celery_app import celery_app; "
                "celery_app.start(['beat','--loglevel=INFO','--schedule',"
                + repr(str(directory / "beat-schedule"))
                + "])",
            ],
            env=environment,
            stdout=beat_log,
            stderr=beat_log,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        processes.append(beat)
        deadline = monotonic() + 130
        while True:
            actual = asyncio.run(inspect_job(database, job_id, revision_id))
            if actual["status"] == "SUCCEEDED" and actual["revision_status"] == "READY":
                break
            if recovered.poll() is not None or beat.poll() is not None:
                raise RuntimeError("PROBE_RECOVERY_PROCESS_EXITED")
            if monotonic() >= deadline:
                raise TimeoutError("PROBE_PERIODIC_RECOVERY_TIMEOUT")
            sleep(0.5)
        report.update(
            after_recovery=actual,
            restart_to_indexed_ms=(monotonic() - restart) * 1000,
            first_dispatch_to_indexed_ms=(monotonic() - started) * 1000,
        )
        if (
            not actual["chunk_ids"]
            or actual["chunk_ids"] != report["before_recovery"]["chunk_ids"]
            or actual["attempt_count"] < 2
        ):
            raise RuntimeError("PROBE_RECOVERY_CHUNK_IDENTITY_MISMATCH")
        report["status"] = "PASS"
    except Exception as error:
        report.update(status="FAIL", failure_class=type(error).__name__, failure_code=str(error))
        raise
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        for handle in handles:
            handle.close()
        report["owned_processes_stopped"] = all(p.poll() is not None for p in processes)
        report["process_exit_codes"] = [p.returncode for p in processes]
        write_json_atomic(output, report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", choices=["crash", "recover"])
    args = parser.parse_args()
    if args.child:
        child(args.directory, args.child == "crash")
    elif args.output:
        run(args.directory, args.output)
    else:
        parser.error("supervisor requires --output")
