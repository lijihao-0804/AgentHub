"""Offline paired replay on the first 24 already exposed, archived MuSiQue questions.

Uses a real October selector response only when its original messages match byte-for-byte.
No hosted model or credentials are used. A/D ranks never see labels.
"""

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

from benchmarks.public_rag.consumer_replay import (
    CONTEXT,
    KB,
    SNAPSHOT,
    CachedConsumerRetriever,
    _Session,
    _spec,
    definition,
    recover_records,
    score_records,
)
from benchmarks.public_rag.evidence_plan_replay import admit
from benchmarks.public_rag.musique_data import load_musique
from benchmarks.public_rag.scenario_runner import chunks
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.contracts import RerankCandidate, RetrievalQuery
from packages.knowledge.evidence_sentence import SentenceEvidenceReranker
from packages.knowledge.evidence_set import EvidenceSetSelector
from packages.knowledge.evidence_set_retriever import EvidenceSetKnowledgeRetriever
from packages.model_gateway.contracts import ModelCapabilities, ModelResponse
from packages.tools.builtins.search_knowledge import search_knowledge
from packages.tools.contracts import ToolExecutionContext

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = (
    ROOT
    / "benchmarks/public_rag/results/published/product-evidence-20261114/selection-receipts.zip"
)
MODEL = (
    ROOT.parent
    / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
)


class ReceiptGateway:
    def __init__(self, events):
        self.events = events
        self.calls = 0

    async def generate_resolved(self, context, plan, request):
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        if messages != self.events["dispatch"]["messages"] or self.calls:
            raise ValueError("selector request does not match its unique historical dispatch")
        self.calls += 1
        return ModelResponse(
            self.events["outcome"]["api"]["content"], "archived", plan.primary.model
        )


async def run(args):
    with ZipFile(RECEIPTS) as archive:
        snapshots = json.loads(archive.read("input-snapshot.json"))
        events = {}
        for event in json.loads(archive.read("selection-events.json")):
            events.setdefault(event["query_id"], {})[event["event"]] = event
        prepared = {r["query_id"]: r for r in json.loads(archive.read("prepared.json"))}
    data = load_musique(ROOT / "data/external/rag-public-20261021/musique/extracted/data", "dev")
    labels = {q["query_id"]: q for q in data["queries"]}
    tool = definition()
    plan = _spec({}).model_plan
    plan = replace(
        plan,
        primary=replace(plan.primary, capabilities=ModelCapabilities(max_context_tokens=32768)),
    )
    model = BgeReranker(model_name=str(args.model.resolve(strict=True)), device=args.device)
    results = []
    for index, snapshot in enumerate(snapshots[:24], 1):
        row, units, question = (
            snapshot["candidate_row"],
            snapshot["source_units"],
            snapshot["question"],
        )
        qid = row["query_id"]
        native = chunks(units, True, 900, 150)
        mapping = {
            c["chunk_id"]: {**c, "chunk_id": str(UUID(int=index * 1000000 + i + 1))}
            for i, c in enumerate(native)
        }
        ranked = [
            {**mapping[c["chunk_id"]], "rerank_score": c["rerank_score"]}
            for c in row["arms"]["heading_fill"]["rerank_ranking"]
        ]
        retriever = CachedConsumerRetriever(ranked, units, id_offset=index * 10000)
        pool = (
            await retriever.retrieve_with_trace(
                CONTEXT,
                RetrievalQuery(
                    question,
                    str(KB),
                    str(SNAPSHOT),
                    dense_top_k=20,
                    sparse_top_k=20,
                    candidate_top_k=20,
                    final_top_k=20,
                ),
            )
        ).evidence
        gateway = ReceiptGateway(events[qid])
        selector = EvidenceSetSelector(gateway, plan)
        start = time.perf_counter()
        scores = SentenceEvidenceReranker(model).inspect_with_whole(
            question,
            [RerankCandidate(e.chunk_id, e.text) for e in pool],
            [e.rerank_score for e in pool],
        )
        elapsed = time.perf_counter() - start
        by_id = {c["chunk_id"]: c for c in ranked}
        reranked = [{**by_id[s.chunk_id], "rerank_score": s.score} for s in scores]
        d = CachedConsumerRetriever(reranked, units, id_offset=index * 10000)
        rec = {
            "query_id": qid,
            "query": question,
            "pool_ids": [e.chunk_id for e in pool],
            "D_window_seconds": round(elapsed, 4),
            "C_historical": {
                k: v
                for k, v in prepared[qid]["selection"].items()
                if k in {"status", "usage", "request_ms"}
            },
            "arms": {},
        }
        for arm, impl in [
            ("B", retriever),
            ("C", EvidenceSetKnowledgeRetriever(retriever, selector)),
            ("D", d),
        ]:
            if arm == "C" and rec["C_historical"]["status"] != "VALID":
                failure = events[qid]["validated"]["api"]
                if failure.get("status") != "PROVIDER_ERROR" or failure.get("http_status") != 400:
                    raise ValueError("unresolved selector outcome cannot fall back")
                impl = retriever  # historical registered fallback on known selector HTTP rejection
            payload = await search_knowledge(
                ToolExecutionContext(CONTEXT, UUID(int=9), "consumer-retrieval"),
                tool,
                {"query": question, "limit": 5},
                _Session,
                retriever=impl,
            )
            observed, final, messages, budget, *_ = await admit(question, payload, tool)
            records = recover_records(final)
            ids = [r["chunk_id"] for r in records]
            if (
                arm in ("B", "C")
                and ids
                != prepared[qid]["arms"][{"B": "legacy5", "C": "set_rank5"}[arm]][
                    "admitted_chunk_ids"
                ]
            ):
                raise ValueError("source or admission parity drift")
            rec["arms"][arm] = {
                "tool_ids": [r["chunk_id"] for r in payload["results"]],
                "observed_ids": [r["chunk_id"] for r in recover_records(observed)],
                "final_ids": ids,
                "model_input_sha256": hashlib.sha256(
                    json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
                "budget": budget,
                "metrics": score_records(records, by_id, units, labels[qid]),
            }
        rec["C_verified_dispatches"] = gateway.calls
        results.append(rec)
    return {
        "scope": (
            "Exposed MuSiQue DEV, first 24 archived cases in file order; selection-only. "
            "A's historical admitted IDs verified against B for every case."
        ),
        "new_provider_calls": 0,
        "final_top_k": 5,
        "context_budget_bytes": 8000,
        "local_model": args.model.name,
        "device": args.device,
        "rows": results,
        "complete_evidence_sets": {
            arm: sum(r["arms"][arm]["metrics"]["complete_evidence_set"] for r in results)
            for arm in ("B", "C", "D")
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=MODEL)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()
