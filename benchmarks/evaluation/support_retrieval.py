"""DEV-only policy retrieval using existing production adapters and cached models."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from benchmarks.evaluation.interview_draft import build_draft
from benchmarks.evaluation.live_pilot import ISOLATED_URL
from benchmarks.retrieval.runner import run_real_benchmark
from benchmarks.retrieval.schema import (
    BenchmarkCase,
    CorpusDocument,
    CorpusSection,
    GroundTruth,
    RetrievalDataset,
)
from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.knowledge.contracts import RetrievalStrategy


def dev_dataset() -> RetrievalDataset:
    draft = build_draft()
    cases = [
        c
        for c in draft["cases"]
        if c["split"] == "DEV"
        and c["category"]
        in {
            "single_hop",
            "multi_hop",
            "unanswerable",
        }
    ]
    allowed = {source for c in cases for source in c["source_ids"]}
    sources = {s["source_id"]: s for s in draft["sources"] if s["source_id"] in allowed}
    corpus = tuple(
        CorpusDocument(
            document_key=s["source_id"],
            revision_key=s["source_id"] + "-revision",
            title=s["source_group"],
            sections=(CorpusSection("policy", s["text"]),),
        )
        for s in sources.values()
    )
    queries = tuple(
        BenchmarkCase(
            case_id=c["case_id"],
            split="dev",
            query=c["user_turns"][0],
            ground_truth=tuple(
                GroundTruth(
                    document_key=source_id,
                    revision_key=source_id + "-revision",
                    locator={"type": "section", "section_key": "policy"},
                    relevant_text=sources[source_id]["text"],
                )
                for source_id in c["source_ids"]
            ),
        )
        for c in cases
    )
    payload = {"corpus": [asdict(d) for d in corpus], "cases": [asdict(c) for c in queries]}
    return RetrievalDataset(
        "support-dev-policy-retrieval-v1", corpus, queries, canonical_json_hash(payload)
    )


async def run(output: Path, summary: Path):
    if output.exists() or summary.exists():
        raise ValueError("OUTPUT_ALREADY_EXISTS")
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        raise ValueError("CACHED_MODELS_ONLY_REQUIRES_HF_HUB_OFFLINE_1")
    dataset = dev_dataset()
    if any(c.split != "dev" for c in dataset.cases):
        raise ValueError("DEVELOPMENT_CANNOT_CONSUME_HOLDOUT")
    settings = Settings().model_copy(
        update={
            "database_url": ISOLATED_URL,
            "knowledge_qdrant_collection": "agenthub_mi34_" + uuid4().hex,
        }
    )
    payload = await run_real_benchmark(
        dataset=dataset,
        settings=settings,
        output=output,
        summary=summary,
        strategies=(RetrievalStrategy.DENSE, RetrievalStrategy.HYBRID_RERANK),
    )
    payload["scope"] = {
        "purpose": "DEVELOPMENT",
        "formal_experiment": False,
        "holdout_consumed": 0,
        "case_count": len(dataset.cases),
        "source_count": len(dataset.corpus),
        "collection": settings.knowledge_qdrant_collection,
        "source_draft_hash": build_draft()["draft_hash"],
        "annotation_kind": "authored_source_identity",
        "boundary": "policy retrieval relevance, not answer correctness or refusal scoring",
    }
    # The legacy formatter uses zero for an empty split. This DEV-only report
    # must keep the unused HOLDOUT unknown, rather than presenting a zero score.
    rows = []
    for strategy, result in payload["strategies"].items():
        result["metrics"]["holdout"] = {"status": "NOT_AVAILABLE", "sample_count": 0}
        result["metrics"]["dev"]["sample_count"] = len(dataset.cases)
        scores = result["metrics"]["dev"]
        rows.append(
            f"| {strategy} | {scores['candidate_recall_at_20']:.4f} | "
            f"{scores['final_recall_at_5']:.4f} | {scores['mrr_at_5']:.4f} | "
            f"{scores['latency_p95_ms']:.3f} |"
        )
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary.write_text(
        "\n".join(
            [
                "# Support DEV policy retrieval",
                "",
                "DEV-only: 24 queries, 8 fictional policies. No HOLDOUT exposure.",
                "This measures policy retrieval; it does not measure answer quality.",
                "",
                "| Strategy | Candidate Recall@20 | Final Recall@5 | MRR@5 | p95 ms |",
                "| --- | ---: | ---: | ---: | ---: |",
                *rows,
                "",
                "HOLDOUT: NOT_AVAILABLE (0 samples).",
                "",
                f"Dataset hash: `{dataset.dataset_hash}`",
                "",
                f"Collection: `{settings.knowledge_qdrant_collection}`",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    configure_windows_asyncio_policy()
    asyncio.run(run(args.output, args.summary))


if __name__ == "__main__":
    main()
