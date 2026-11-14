"""Replay archived public terminal answers through the citation HTTP handler without network."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from apps.api.routes import citation_qa as route
from benchmarks.public_rag.qa_first_response_review import index, load, save
from benchmarks.public_rag.qa_first_response_verify import canon, events_of, require, sha
from packages.knowledge.citation_qa import CitationQaService, HybridKnowledgeRetriever
from packages.knowledge.contracts import (
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceStage,
    RetrievedEvidence,
)
from tests.unit.test_knowledge_m3e_api import _app
from tests.unit.test_knowledge_m3e_citation_qa import _FakeModelGateway, _FakeSession

PREVIOUS = Path("benchmarks/public_rag/results/published/evidence-retention-20261114")
CURRENT = PREVIOUS.with_name("refusal-consistency-20261114")


def archived_retrieval(row, snapshot):
    (reader,) = events_of(row, "reader")
    evidence = tuple(
        RetrievedEvidence(
            document_id=item["document_id"],
            document_revision_id=item["document_revision_id"],
            chunk_id=item["chunk_id"],
            source=item["source"],
            locator=item["locator"],
            text=item["text"],
            metadata=item["lifecycle"],
            retrieval_score=0.0,
            rerank_score=None,
        )
        for item in reader["entries"]
    )
    stage = RetrievalTraceStage(0.0, ())
    return RetrievalResult(evidence, RetrievalTrace(snapshot, stage, stage, stage, stage, 0.0))


def public_answer(body):
    return {
        "answer": body["answer"],
        "insufficient": body["insufficient_evidence"],
        "citations": [
            [
                c["id"],
                c["document_id"],
                c["document_revision_id"],
                c["chunk_id"],
                c["source"],
                c["locator"],
                c["excerpt"],
            ]
            for c in body["citations"]
        ],
    }


def archived_answer(row):
    return {
        "answer": row["answer"],
        "insufficient": row["insufficient"],
        "citations": [
            [
                c["number"],
                c["document_id"],
                c["revision"],
                c["chunk_id"],
                c["citation_source"],
                c["locator"],
                c["excerpt"],
            ]
            for c in row["citations"]
        ],
    }


async def replay(previous, *, baseline_only=False):
    cases = index(load(previous / "protocol.json")["cases"])
    grades = load(previous / "review.json")["rows"]
    gateway = _FakeModelGateway()
    app = _app(_FakeSession(), gateway)
    report = []
    retrieve = AsyncMock()
    with patch.object(HybridKnowledgeRetriever, "retrieve_with_trace", retrieve):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://replay"
        ) as client:
            for scope in ("independent", "historical"):
                for arm, label in (("A", "baseline"), ("B", "candidate")):
                    receipt = load(previous / "live" / f"{arm}-{scope}.json")
                    for row in receipt["rows"]:
                        cid = row["id"]
                        case, grade = cases[cid], grades[cid][label]["verdict"]
                        prior_correct = grade == ("refusal" if case["negative"] else "full")
                        record = dict(
                            id=cid,
                            scope=scope,
                            previous_arm=arm,
                            previous_grade=grade,
                            prior_correct=prior_correct,
                            http=row["http"],
                        )
                        report.append(record)
                        if row["http"] != 200:
                            continue
                        original = archived_answer(row)
                        model_output = {
                            "answer": row["answer"],
                            "citation_ids": [c["number"] for c in row["citations"]],
                            "insufficient_evidence": row["insufficient"],
                        }
                        gateway.content = json.dumps(model_output)
                        retrieve.return_value = archived_retrieval(row, receipt["snapshot"])
                        payload = {
                            "workspace_id": receipt["workspace"],
                            "knowledge_base_id": receipt["knowledge_base"],
                            "knowledge_snapshot_id": receipt["snapshot"],
                            "model_profile_id": receipt["profile"],
                            "query": case["query"],
                            "output_mode": receipt["output_mode"],
                        }
                        for fixed in [False] if baseline_only else [False, True]:
                            service = (
                                partial(CitationQaService, verify_refusal_consistency=True)
                                if fixed
                                else CitationQaService
                            )
                            gateway.requests.clear()
                            with patch.object(route, "CitationQaService", service):
                                response = await client.post(
                                    "/api/v1/knowledge/query", json=payload
                                )
                            require(
                                response.status_code == 200, f"{cid}: replay {response.text[:200]}"
                            )
                            require(
                                len(gateway.requests) == 1,
                                f"{cid}: unrecorded follow-up generation",
                            )
                            result = public_answer(response.json())
                            require(
                                sha(gateway.requests[0].messages[1].content)
                                == events_of(row, "reader")[0]["prompt_sha256"],
                                f"{cid}: reconstructed reader prompt differs",
                            )
                            if not fixed:
                                require(
                                    result == original, f"{cid}: previous public answer differs"
                                )
                                record["previous_sha256"] = sha(canon(original))
                            else:
                                record["replayed_sha256"] = sha(canon(result))
                                record["unchanged"] = result == original
                                if result != original:
                                    record["new_answer"] = result["answer"]
                                    record["new_insufficient"] = result["insufficient"]
                                    record["new_citation_count"] = len(result["citations"])
    return {
        "input": "Public terminal response fields reconstructed into strict model JSON. Original "
        "provider bytes and retrieval scores were not archived. Historical HTTP errors have no "
        "terminal answer for replay. Retrieval and model boundaries use archived fixtures.",
        "archived": len(report),
        "returned": sum(r["http"] == 200 for r in report),
        "prior_correct": sum(r["prior_correct"] for r in report),
        "prior_correct_unchanged": sum(
            r.get("unchanged", False) and r["prior_correct"] for r in report
        ),
        "changed_by_previous_verdict": dict(
            Counter(r["previous_grade"] for r in report if r.get("unchanged") is False)
        ),
        "rows": report,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, default=PREVIOUS)
    parser.add_argument("--output", type=Path, default=CURRENT / "historical-replay.json")
    parser.add_argument("--baseline-only", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(replay(args.previous, baseline_only=args.baseline_only))
    if not args.baseline_only:
        save(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"rows", "input"}}))


if __name__ == "__main__":
    main()
