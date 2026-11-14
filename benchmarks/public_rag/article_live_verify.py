"""Reconstruct exact returned evidence from public corpus files and verify every receipt digest."""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from benchmarks.public_rag.article_live_replay import project
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.parser import ParsedBlock, ParsedDocument


def verify(receipt, documents):
    storage = {}
    for row in receipt["rows"]:
        for c in row.get("citations", []):
            key = c["source"]
            if key not in storage:
                parsed = ParsedDocument((ParsedBlock(documents[key], {"type": "text_range"}),))
                storage[key] = build_deterministic_chunks(
                    UUID(int=0), parsed, chunk_size_chars=1200, chunk_overlap_chars=200
                )
            original = storage[key][c["ordinal"]].text
            assert hashlib.sha256(original.encode()).hexdigest() == c["chunk_text_sha256"]
            quote = original[c["excerpt_start_in_chunk"] : c["excerpt_end_in_chunk"]]
            assert quote.startswith(c["excerpt_preview"])
            assert hashlib.sha256(quote.encode()).hexdigest() == c["excerpt_sha256"]
            yield {"case": row["id"], "citation": c["number"], "document": key, "excerpt": quote}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--qasper-train", type=Path)
    parser.add_argument("--scifact-corpus", type=Path)
    args = parser.parse_args()
    if bool(args.qasper_train) == bool(args.scifact_corpus):
        parser.error("choose one corpus")
    if args.qasper_train:
        docs = project(args.qasper_train)[2]
    else:
        with args.scifact_corpus.open(encoding="utf8") as rows:
            docs = {
                r["_id"]: r["title"].strip() + "\n\n" + r["text"].strip()
                for line in rows
                if (r := json.loads(line))
            }
    for record in verify(json.loads(args.receipt.read_bytes()), docs):
        print(json.dumps(record, ensure_ascii=True))


if __name__ == "__main__":
    main()
