"""Reobserve four previously scored refusals and the ten original positive controls."""

import argparse
import asyncio
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.engine import make_url

from benchmarks.retrieval.revision_reuse import digest
from benchmarks.retrieval.table_header_answers import write_entry
from benchmarks.retrieval.table_header_cloud_qa import CloudTableMeter
from benchmarks.retrieval.table_headers import PROTOCOL, arm
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.knowledge import citation_qa
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder


async def run(args):
    protocol = json.loads(PROTOCOL.read_text(encoding="utf8"))
    tables = re.findall(
        r"<table\b[^>]*>.*?</table>", args.page.read_text(encoding="utf8"), re.I | re.S
    )
    if [digest(table.encode()) for table in tables] != protocol["table_sha256"]:
        raise ValueError("The cloud diagnostic requires the two frozen public WAI tables.")
    payload = json.dumps(
        [{"page_idx": i, "type": "table", "table_body": table} for i, table in enumerate(tables)]
    ).encode()
    start = 10 if args.arm == "baseline" else 0
    if digest(citation_qa._PROMPT_JSON_SYSTEM.encode()) != (
        "9921923abe84909a433d1333667a6cb16f7ba0928080ec0b21136d1b12238499"
    ):
        raise ValueError("The historical prompt fingerprint changed.")
    if args.arm == "candidate" and digest(
        citation_qa.citation_system_prompt("PROMPT_JSON").encode()
    ) != ("0ac09ac99b732e92e8ae8d1d921eb828b17e722427c930a23c4483b71fbe15a6"):
        raise ValueError("The diagnostic candidate prompt fingerprint changed.")
    cases = protocol["cases"][start:]
    dsn = os.environ["AGENTHUB_TEST_DATABASE_URL"]
    target = make_url(dsn)
    if target.host != "127.0.0.1" or target.database != "agenthub_nov18_rag":
        raise ValueError("Use the dedicated loopback WAI database.")
    args.run_dir.mkdir(parents=True, exist_ok=False)
    engine, factory = create_database(dsn)
    meter = CloudTableMeter(args.run_dir / "receipts.jsonl")
    meter.max_calls = 2 * len(cases)
    models = (
        BgeM3DenseEmbedder(model_name=str(args.embedding.resolve(strict=True)), device=args.device),
        BgeM3SparseEncoder(model_name=str(args.embedding.resolve(strict=True))),
        BgeReranker(model_name=str(args.reranker.resolve(strict=True)), device=args.device),
    )
    validator = citation_qa.resolve_citation_answer
    contexts = []
    try:
        with (
            tempfile.TemporaryDirectory() as blobs,
            (args.run_dir / "visible-attempts.jsonl").open("x", encoding="utf8") as log,
            meter.measure(),
        ):
            settings = Settings(
                _env_file=None,
                database_url=dsn,
                blob_root=blobs,
                credential_master_key=secrets.token_urlsafe(32),
                knowledge_qa_answer_contract_v1=False,
                knowledge_qa_contextual_abstention=False,
                knowledge_min_rerank_score=None,
                knowledge_vector_cache_enabled=False,
                knowledge_qa_max_evidence_chars=16000,
                knowledge_chunk_size_chars=1200,
                knowledge_chunk_overlap_chars=200,
                knowledge_superseded_rank_penalty=0.0,
            )

            def observe(query, content, **kwargs):
                record = {"query": query, "wire_call": meter.calls, "visible_content": content}
                try:
                    result = validator(query, content, **kwargs)
                except citation_qa._InvalidCitationAnswer as error:
                    write_entry(log, {**record, "validation": error.reason.value})
                    raise
                write_entry(log, {**record, "validation": "accepted"})
                return result

            prompt = citation_qa.citation_system_prompt
            if args.arm == "baseline":
                # Exact pre-change PROMPT_JSON instructions; schema mode is unused in this replay.
                def prompt(mode, *, answer_contract_v1=False):
                    if mode != "PROMPT_JSON" or answer_contract_v1:
                        raise ValueError("Unexpected historical output mode.")
                    return citation_qa._PROMPT_JSON_SYSTEM

            with (
                patch.object(citation_qa, "resolve_citation_answer", observe),
                patch.object(citation_qa, "citation_system_prompt", prompt),
            ):
                results = await arm(settings, factory, models, payload, cases, contexts, meter)
            receipt = {
                "arm": args.arm,
                "prompt_sha256": digest(prompt("PROMPT_JSON").encode()),
                "cases": results,
                "top5_text_sha256": [
                    digest(json.dumps(rows, sort_keys=True).encode()) for rows in contexts
                ],
            }
            (args.run_dir / "results.json").write_text(
                json.dumps(receipt, indent=2), encoding="utf8"
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=["baseline", "candidate"], required=True)
    for key in ("embedding", "reranker", "page", "run-dir"):
        parser.add_argument("--" + key, required=True, type=Path)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--dispatch", action="store_true", required=True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    asyncio.run(run(parser.parse_args()))
