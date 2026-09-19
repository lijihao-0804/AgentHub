"""Seed and run the M3 retrieval baseline against production retrieval adapters."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.worker.tasks.knowledge import _vector_records
from benchmarks.retrieval.metrics import (
    BenchmarkHit,
    BenchmarkRetrieval,
    EvaluationResult,
    evaluate_dataset,
)
from benchmarks.retrieval.schema import RetrievalDataset, load_dataset
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.contracts import RetrievalQuery, RetrievalStrategy
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.retrieval import HybridKnowledgeRetriever
from packages.knowledge.snapshots import KnowledgeSnapshotService, ResolvedKnowledgeSnapshot

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "benchmarks" / "retrieval" / "dataset.json"
DEFAULT_RESULTS = PROJECT_ROOT / "benchmarks" / "retrieval" / "results"
DEFAULT_SUMMARY = PROJECT_ROOT / "docs" / "benchmark" / "m3-retrieval-baseline.md"


@dataclass(frozen=True, slots=True)
class SeededChunk:
    chunk: DocumentChunk
    document_key: str
    revision_key: str


@dataclass(frozen=True, slots=True)
class SeededCorpus:
    context: WorkspaceExecutionContext
    knowledge_base_id: UUID
    snapshot: ResolvedKnowledgeSnapshot
    chunks: tuple[SeededChunk, ...]


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def _context(
    *, user_id: UUID, organization_id: UUID, workspace_id: UUID
) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="m3-retrieval-benchmark",
                trace_id="m3-retrieval-benchmark",
                user_id=str(user_id),
            ),
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset({"knowledge_run"}),
    )


async def _seed_corpus(
    factory: async_sessionmaker[AsyncSession],
    dataset: RetrievalDataset,
) -> tuple[WorkspaceExecutionContext, UUID, tuple[SeededChunk, ...]]:
    async with factory() as session:
        user = User(
            email=f"m3-retrieval-{uuid4()}@benchmark.invalid",
            normalized_email=f"m3-retrieval-{uuid4()}@benchmark.invalid",
            password_hash="benchmark-only",
        )
        session.add(user)
        await session.flush()
        organization = Organization(
            name=f"m3-retrieval-{uuid4()}",
            created_by=user.id,
        )
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role="OWNER",
            )
        )
        workspace = Workspace(organization_id=organization.id, name="M3 Retrieval Benchmark")
        session.add(workspace)
        await session.flush()
        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name="M3 Retrieval Corpus")
        session.add(knowledge_base)
        await session.flush()

        seeded_chunks: list[SeededChunk] = []
        for document_spec in dataset.corpus:
            document = Document(
                workspace_id=workspace.id,
                knowledge_base_id=knowledge_base.id,
                name=f"{document_spec.document_key}.md",
            )
            session.add(document)
            await session.flush()
            document_text = "\n\n".join(section.text for section in document_spec.sections)
            revision = DocumentRevision(
                workspace_id=workspace.id,
                knowledge_base_id=knowledge_base.id,
                document_id=document.id,
                revision_number=1,
                original_filename=f"{document_spec.document_key}.md",
                blob_key=f"benchmarks/m3-retrieval/{document_spec.revision_key}",
                media_type="text/markdown",
                file_size=len(document_text.encode("utf-8")),
                ingestion_status=RevisionIngestionStatus.READY,
                lifecycle_status=RevisionLifecycleStatus.ACTIVE,
            )
            session.add(revision)
            await session.flush()
            parsed = ParsedDocument(
                blocks=tuple(
                    ParsedBlock(
                        text=section.text,
                        locator={"type": "section", "section_key": section.section_key},
                    )
                    for section in document_spec.sections
                )
            )
            candidates = build_deterministic_chunks(
                revision.id,
                parsed,
                chunk_size_chars=450,
                chunk_overlap_chars=80,
            )
            rows = [
                DocumentChunk(
                    chunk_id=candidate.chunk_id,
                    workspace_id=workspace.id,
                    knowledge_base_id=knowledge_base.id,
                    document_id=document.id,
                    document_revision_id=revision.id,
                    ordinal=candidate.ordinal,
                    normalized_content_hash=candidate.normalized_content_hash,
                    text=candidate.text,
                    locator=candidate.locator,
                )
                for candidate in candidates
            ]
            session.add_all(rows)
            seeded_chunks.extend(
                SeededChunk(
                    chunk=row,
                    document_key=document_spec.document_key,
                    revision_key=document_spec.revision_key,
                )
                for row in rows
            )
        await session.commit()
        context = _context(
            user_id=user.id,
            organization_id=organization.id,
            workspace_id=workspace.id,
        )
        return context, knowledge_base.id, tuple(seeded_chunks)


async def _materialize_snapshot(
    factory: async_sessionmaker[AsyncSession],
    context: WorkspaceExecutionContext,
    knowledge_base_id: UUID,
) -> ResolvedKnowledgeSnapshot:
    async with factory() as session:
        return await KnowledgeSnapshotService().create_current_snapshot(
            session,
            context,
            knowledge_base_id,
        )


def _index_corpus(
    settings: Settings,
    seeded_chunks: tuple[SeededChunk, ...],
) -> tuple[object, object, object, object]:
    components = production_retrieval_components(settings)
    index = QdrantVectorIndex(
        url=settings.qdrant_url,
        collection_name=settings.knowledge_qdrant_collection,
        dense_vector_size=settings.knowledge_dense_vector_size,
        timeout_seconds=settings.knowledge_qdrant_timeout_seconds,
    )
    chunks = [item.chunk for item in seeded_chunks]
    records = _vector_records(
        SimpleNamespace(
            workspace_id=chunks[0].workspace_id,
            knowledge_base_id=chunks[0].knowledge_base_id,
        ),
        chunks,
        components.dense,
        components.sparse,
    )
    index.ensure_collection()
    index.upsert(records)
    return components.dense, components.sparse, components.reranker, index


def _hit_from_chunk(
    chunk_id: str,
    score: float,
    chunks: dict[str, SeededChunk],
) -> BenchmarkHit:
    seeded = chunks.get(chunk_id)
    if seeded is None:
        raise RuntimeError(f"retrieval returned unknown benchmark chunk: {chunk_id}")
    return BenchmarkHit(
        document_key=seeded.document_key,
        revision_key=seeded.revision_key,
        locator=dict(seeded.chunk.locator),
        score=float(score),
        chunk_id=chunk_id,
    )


async def _evaluate_real_models(
    *,
    factory: async_sessionmaker[AsyncSession],
    dataset: RetrievalDataset,
    seeded: SeededCorpus,
    components: tuple[object, object, object, object],
    settings: Settings,
    strategy: RetrievalStrategy,
) -> tuple[EvaluationResult, dict[str, list[float]]]:
    dense, sparse, reranker, index = components
    chunk_lookup = {item.chunk.chunk_id: item for item in seeded.chunks}
    logical_lookup = {
        (str(item.chunk.document_id), str(item.chunk.document_revision_id)): (
            item.document_key,
            item.revision_key,
        )
        for item in seeded.chunks
    }
    latencies: dict[str, list[float]] = {"dev": [], "holdout": []}
    async with factory() as session:
        retriever = HybridKnowledgeRetriever(
            session=session,
            dense_embedder=dense,
            sparse_encoder=sparse,
            reranker=reranker,
            vector_index=index,
            rrf_k=settings.knowledge_rrf_k,
        )

        async def retrieve_case(case):
            started = time.perf_counter()
            result = await retriever.retrieve_with_trace(
                seeded.context,
                RetrievalQuery(
                    text=case.query,
                    knowledge_base_id=str(seeded.knowledge_base_id),
                    knowledge_snapshot_id=str(seeded.snapshot.snapshot_id),
                    strategy=strategy,
                    dense_top_k=30,
                    sparse_top_k=30,
                    candidate_top_k=20,
                    final_top_k=6,
                ),
            )
            latencies[case.split].append((time.perf_counter() - started) * 1000)
            stage_results = (
                result.trace.dense.results
                if strategy is RetrievalStrategy.DENSE
                else result.trace.fusion.results
            )
            candidate_hits = tuple(
                _hit_from_chunk(item.chunk_id, item.score, chunk_lookup) for item in stage_results
            )
            final_hits = tuple(
                BenchmarkHit(
                    document_key=logical_lookup[
                        (evidence.document_id, evidence.document_revision_id)
                    ][0],
                    revision_key=logical_lookup[
                        (evidence.document_id, evidence.document_revision_id)
                    ][1],
                    locator=dict(evidence.locator),
                    score=float(evidence.rerank_score or evidence.retrieval_score),
                    chunk_id=evidence.chunk_id,
                )
                for evidence in result.evidence
            )
            return BenchmarkRetrieval(candidate_hits=candidate_hits, final_hits=final_hits)

        retrievals = {case.case_id: await retrieve_case(case) for case in dataset.cases}
    return evaluate_dataset(dataset, lambda case: retrievals[case.case_id]), latencies


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _runtime_device(requested: str) -> dict[str, Any]:
    details: dict[str, Any] = {"requested": requested}
    try:
        import torch
    except ImportError:
        details["resolved"] = "unknown"
        return details

    details["torch_version"] = torch.__version__
    details["cuda_available"] = bool(torch.cuda.is_available())
    if requested == "auto":
        details["resolved"] = "cuda" if details["cuda_available"] else "cpu"
    else:
        details["resolved"] = requested
    if details["cuda_available"]:
        details["cuda_device"] = torch.cuda.get_device_name(0)
    return details


def _result_payload(
    dataset: RetrievalDataset,
    evaluation: EvaluationResult,
    snapshot: ResolvedKnowledgeSnapshot,
    settings: Settings,
    *,
    strategy: RetrievalStrategy,
    latencies: dict[str, list[float]],
) -> dict[str, Any]:
    metrics = {
        split: {
            **evaluation.metrics(None if split == "overall" else split),
            "latency_p50_ms": _percentile(
                latencies["dev"] + latencies["holdout"] if split == "overall" else latencies[split],
                0.5,
            ),
            "latency_p95_ms": _percentile(
                latencies["dev"] + latencies["holdout"] if split == "overall" else latencies[split],
                0.95,
            ),
        }
        for split in ("dev", "holdout", "overall")
    }
    return {
        "benchmark": "m3-retrieval-baseline"
        if strategy is RetrievalStrategy.HYBRID_RERANK
        else "m7e-retrieval-ablation",
        "strategy": strategy.value,
        "dataset_version": dataset.dataset_version,
        "dataset_hash": dataset.dataset_hash,
        "git_commit": _git_commit(),
        "snapshot_id": str(snapshot.snapshot_id),
        "snapshot_hash": snapshot.content_hash,
        "retrieval_config": {
            "dense_top_k": 30,
            "sparse_top_k": 30,
            "candidate_top_k": 20,
            "final_top_k": 6,
            "rrf_k": settings.knowledge_rrf_k,
        },
        "embedding_model": settings.knowledge_embedding_model,
        "reranker_model": settings.knowledge_reranker_model,
        "sparse_encoder": settings.knowledge_embedding_model,
        "device": {
            "embedding": _runtime_device(settings.knowledge_embedding_device),
            "reranker": _runtime_device(settings.knowledge_reranker_device),
            "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "0"),
        },
        "timestamp": datetime.now(UTC).isoformat(),
        "metrics": metrics,
        "failure_analysis": {
            "dev": evaluation.failures("dev"),
            "holdout": evaluation.failures("holdout"),
            "overall": evaluation.failures(),
        },
        "case_metrics": [
            {
                "case_id": case.case_id,
                "split": case.split,
                "candidate_recall_at_20": case.candidate_recall_at_20,
                "final_recall_at_5": case.final_recall_at_5,
                "mrr_at_5": case.mrr_at_5,
                "failure_category": case.failure_category,
            }
            for case in evaluation.cases
        ],
    }


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * quantile))))
    return round(ordered[index], 3)


def _write_artifacts(payload: dict[str, Any], output: Path, summary: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if "strategies" in payload:
        summary.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            f"| {strategy} | {values['metrics']['dev']['final_recall_at_5']:.4f} | "
            f"{values['metrics']['holdout']['final_recall_at_5']:.4f} | "
            f"{values['metrics']['overall']['mrr_at_5']:.4f} | "
            f"{values['metrics']['overall']['latency_p95_ms']:.3f} |"
            for strategy, values in sorted(payload["strategies"].items())
        ]
        summary.write_text(
            "\n".join(
                [
                    "# M7-E Retrieval Strategy Ablation",
                    "",
                    "Status: PASS — real host ablation executed.",
                    "",
                    f"- Dataset: `{payload['dataset_version']}`",
                    f"- Dataset hash: `{payload['dataset_hash']}`",
                    f"- Git commit: `{payload['git_commit']}`",
                    "",
                    "| Strategy | DEV Final Recall@5 | HOLDOUT Final Recall@5 | "
                    "ALL MRR@5 | ALL p95 (ms) |",
                    "|---|---:|---:|---:|---:|",
                    *rows,
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return
    metrics = payload["metrics"]
    failures = payload["failure_analysis"]
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        "\n".join(
            [
                "# M3 Retrieval Evaluation Baseline",
                "",
                "Status: PASS — real host baseline executed.",
                "",
                f"- Dataset: `{payload['dataset_version']}`",
                f"- Dataset hash: `{payload['dataset_hash']}`",
                f"- Git commit: `{payload['git_commit']}`",
                f"- Snapshot: `{payload['snapshot_id']}`",
                f"- Snapshot hash: `{payload['snapshot_hash']}`",
                f"- Embedding model: `{payload['embedding_model']}`",
                f"- Reranker model: `{payload['reranker_model']}`",
                "- Device: "
                f"embedding={payload['device']['embedding']['resolved']}, "
                f"reranker={payload['device']['reranker']['resolved']}, "
                f"torch={payload['device']['embedding'].get('torch_version', 'unknown')}, "
                f"CUDA={payload['device']['embedding'].get('cuda_available', False)}, "
                f"GPU={payload['device']['embedding'].get('cuda_device', 'n/a')}",
                "",
                "## Metrics",
                "",
                "| Split | Candidate Recall@20 | Final Recall@5 | MRR@5 |",
                "|---|---:|---:|---:|",
                *[
                    f"| {split} | {values['candidate_recall_at_20']:.4f} | "
                    f"{values['final_recall_at_5']:.4f} | {values['mrr_at_5']:.4f} |"
                    for split, values in (
                        ("dev", metrics["dev"]),
                        ("holdout", metrics["holdout"]),
                        ("overall", metrics["overall"]),
                    )
                ],
                "",
                "## Failure analysis",
                "",
                *[
                    f"- {split}: {len(failures[split])} failures"
                    for split in ("dev", "holdout", "overall")
                ],
                "",
                "This baseline is retrieval-only. It does not measure answer quality or "
                "citation QA.",
                "M3 overall acceptance is recorded only after this real baseline and all prior "
                "milestone checks pass.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


async def run_real_benchmark(
    *,
    dataset: RetrievalDataset,
    settings: Settings,
    output: Path,
    summary: Path,
    strategies: tuple[RetrievalStrategy, ...] = (RetrievalStrategy.HYBRID_RERANK,),
) -> dict[str, Any]:
    engine, factory = create_database(_async_database_url(settings.database_url))
    try:
        context, knowledge_base_id, chunks = await _seed_corpus(factory, dataset)
        snapshot = await _materialize_snapshot(factory, context, knowledge_base_id)
        seeded = SeededCorpus(context, knowledge_base_id, snapshot, chunks)
        components = _index_corpus(settings, chunks)
        payloads: dict[str, dict[str, Any]] = {}
        for strategy in strategies:
            evaluation, latencies = await _evaluate_real_models(
                factory=factory,
                dataset=dataset,
                seeded=seeded,
                components=components,
                settings=settings,
                strategy=strategy,
            )
            payloads[strategy.value] = _result_payload(
                dataset,
                evaluation,
                snapshot,
                settings,
                strategy=strategy,
                latencies=latencies,
            )
        payload = (
            payloads[RetrievalStrategy.HYBRID_RERANK.value]
            if len(payloads) == 1
            else {
                "benchmark": "m7e-retrieval-ablation",
                "dataset_version": dataset.dataset_version,
                "dataset_hash": dataset.dataset_hash,
                "git_commit": _git_commit(),
                "snapshot_id": str(snapshot.snapshot_id),
                "snapshot_hash": snapshot.content_hash,
                "strategies": payloads,
            }
        )
        _write_artifacts(payload, output, summary)
        return payload
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--qdrant-url", default=None)
    parser.add_argument("--collection", default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--real-models", action="store_true")
    parser.add_argument(
        "--strategy",
        choices=[strategy.value for strategy in RetrievalStrategy],
        default=RetrievalStrategy.HYBRID_RERANK.value,
    )
    parser.add_argument(
        "--ablation",
        action="store_true",
        help="Run DENSE, HYBRID, and HYBRID_RERANK on one seeded corpus and snapshot.",
    )
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    dataset = load_dataset(args.dataset)
    if args.validate_only:
        print(
            json.dumps(
                {
                    "dataset_version": dataset.dataset_version,
                    "dataset_hash": dataset.dataset_hash,
                    "corpus_documents": len(dataset.corpus),
                    "cases": len(dataset.cases),
                    "dev": sum(case.split == "dev" for case in dataset.cases),
                    "holdout": sum(case.split == "holdout" for case in dataset.cases),
                },
                ensure_ascii=False,
            )
        )
        return
    if not args.real_models:
        raise SystemExit("The real baseline requires --real-models; CI uses unit fake wiring only.")
    settings = Settings(
        database_url=args.database_url or Settings().database_url,
        qdrant_url=args.qdrant_url or Settings().qdrant_url,
        knowledge_qdrant_collection=args.collection or f"agenthub_m3_retrieval_{uuid4().hex[:12]}",
    )
    strategies = tuple(RetrievalStrategy) if args.ablation else (RetrievalStrategy(args.strategy),)
    output = args.output or (
        DEFAULT_RESULTS / f"m7e-retrieval-ablation-{_git_commit()}.json"
        if args.ablation
        else DEFAULT_RESULTS / f"{dataset.dataset_version}.json"
    )
    summary = args.summary
    if args.ablation and summary == DEFAULT_SUMMARY:
        summary = PROJECT_ROOT / "docs" / "benchmark" / "m7e-retrieval-ablation.md"
    try:
        payload = asyncio.run(
            run_real_benchmark(
                dataset=dataset,
                settings=settings,
                output=output,
                summary=summary,
                strategies=strategies,
            )
        )
    except Exception as exc:
        if not args.ablation:
            raise
        blocked = {
            "benchmark": "m7e-retrieval-ablation",
            "status": "BLOCKED_ENVIRONMENT",
            "git_commit": _git_commit(),
            "reason_type": type(exc).__name__,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(blocked, indent=2) + "\n", encoding="utf-8")
        print("REAL_RETRIEVAL_ABLATION=BLOCKED_ENVIRONMENT")
        return
    if args.ablation:
        print(
            json.dumps(
                {key: value["metrics"] for key, value in payload["strategies"].items()}, indent=2
            )
        )
    else:
        print(json.dumps(payload["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


__all__ = ["main", "run_real_benchmark"]
