"""Compare production chunk boundaries and paper-local BM25 on QASPER TRAIN."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from benchmarks.public_rag import paper_qa_data
from benchmarks.public_rag.candidate_recall_methods import WordBM25
from benchmarks.public_rag.paper_qa_live import render_paper
from packages.core.config.settings import Settings
from packages.knowledge.chunking_policy import ChunkingPolicy, ChunkingStrategy
from packages.knowledge.parser import ParsedBlock, ParsedDocument


def replay() -> dict:
    # Freeze the label-blind selection so replay does not require the local exposure ledgers.
    manifest = json.loads(Path(__file__).with_name("chunk_strategy_sample.json").read_text())
    source = Path("data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json")
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest["dataset_sha256"]:
        raise ValueError("QASPER TRAIN source differs from the frozen sample")
    papers = json.loads(payload)
    settings = Settings(knowledge_chunk_size_chars=1200, knowledge_chunk_overlap_chars=200)
    counts = Counter()
    hits = {strategy: {"intact": set(), "bm25_top6": set()} for strategy in ChunkingStrategy}
    scored = set()
    sample_count = 0
    for paper_id in manifest["paper_ids"]:
        raw = papers[paper_id]
        paper = paper_qa_data._corpus_paper(paper_id, raw)
        text, _ = render_paper(paper)
        revision = uuid5(NAMESPACE_URL, f"qasper:{paper_id}")
        parsed = ParsedDocument(
            (ParsedBlock(text, {"type": "text_range", "char_start": 0, "char_end": len(text)}),)
        )
        questions = []
        for qa in raw["qas"]:
            case = paper_qa_data._case(paper_id, qa)
            sample_count += 1
            if case["consensus"] != "answerable":
                continue
            gold = {
                unicodedata.normalize("NFC", span).strip()
                for annotation in case["annotations"]
                for span in annotation["evidence"]
            }
            gold = {span for span in gold if span and span in text}
            if gold:
                questions.append((case, gold))
                scored.add(case["query_id"])
        for strategy in ChunkingStrategy:
            policy = ChunkingPolicy.resolve(strategy, "text/markdown", settings)
            chunks = policy.build(revision, parsed, media_type="text/markdown")
            by_id = {item.chunk_id: (paper_id, item.text) for item in chunks}
            index = WordBM25(by_id)
            counts[strategy] += len(chunks)
            for case, gold in questions:
                exact = {
                    key
                    for key, (_, content) in by_id.items()
                    if any(span in content for span in gold)
                }
                if exact:
                    hits[strategy]["intact"].add(case["query_id"])
                if any(hit["chunk_id"] in exact for hit in index.query(case["question"], limit=6)):
                    hits[strategy]["bm25_top6"].add(case["query_id"])
    baseline = hits[ChunkingStrategy.FIXED]
    return {
        "seed": manifest["seed"],
        "papers": len(manifest["paper_ids"]),
        "sampled": sample_count,
        "answerable_with_exact_source_evidence": len(scored),
        "strategies": {
            key: {
                "chunks": counts[key],
                **{
                    metric: {
                        "hits": len(ids),
                        "gained_vs_fixed": sorted(ids - baseline[metric]),
                        "lost_vs_fixed": sorted(baseline[metric] - ids),
                    }
                    for metric, ids in metrics.items()
                },
            }
            for key, metrics in hits.items()
        },
    }


if __name__ == "__main__":
    print(json.dumps(replay(), indent=2))
