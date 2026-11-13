"""Frozen TRAIN-only evidence and document-disjoint cohorts for candidate recall."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from benchmarks.public_rag.data import SourceDocument, query_groups
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.parser import ParsedBlock, ParsedDocument

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = Path(__file__).parent / "results/published/candidate-recall-20261114"
PROTOCOL = PUBLIC / "protocol.json"
DATA = ROOT / "data/external/rag-public-20261021/scifact/extracted/scifact"
HISTORY = Path(__file__).parent / "results/parent-context-20261022"
SNAPSHOT = ROOT / ".scratch/rag-opt-20261021/scifact-state.json"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fingerprint(values: list[str]) -> str:
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def jsonl(path: Path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def verified_protocol() -> dict:
    protocol = json.loads(PROTOCOL.read_bytes())
    for name, digest in protocol["inputs"].items():
        if sha256(ROOT / name) != digest:
            raise ValueError(f"evidence fingerprint mismatch: {name}")
    meta = json.loads((HISTORY / "dev-baseline.meta.json").read_bytes())
    if (meta["split"], meta["queries"], meta["snapshot_id"]) != (
        "train",
        protocol["split_plan"]["query_count"],
        protocol["snapshot_id"],
    ):
        raise ValueError("historical candidate provenance differs")
    return protocol


def train_qrels(only: set[str] | None = None) -> dict[str, dict[str, int]]:
    labels: dict[str, dict[str, int]] = defaultdict(dict)
    with (DATA / "qrels/train.tsv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            qid = row["query-id"]
            if only is not None and qid not in only:
                continue
            doc = row["corpus-id"]
            grade = int(row["score"])
            if doc in labels[qid] or grade < 0:
                raise ValueError("duplicate or invalid TRAIN judgment")
            labels[qid][doc] = grade
    if only is not None and set(labels) != only:
        raise ValueError("scoring cohort and TRAIN labels differ")
    return dict(labels)


def make_cohorts(ids: set[str], groups: dict[str, str], plan: dict) -> dict[str, list[str]]:
    def order(salt: str, subset):
        return sorted(subset, key=lambda q: hashlib.sha256(f"{salt}:{q}".encode()).digest())

    if len(ids) != plan["query_count"] or set(groups) != ids:
        raise ValueError("TRAIN query populations differ")
    original = order(plan["old_salt"], ids)
    dev_end = plan["development"]
    old_end = dev_end + plan["warmup_excluded"]
    exposed_components = {groups[q] for q in original[:old_end]}
    return {
        "development": original[:dev_end],
        "warmup": original[dev_end:old_end],
        "excluded_shared_source": [
            q for q in original[old_end:] if groups[q] in exposed_components
        ],
        "confirmation": order(
            plan["confirmation_salt"],
            (q for q in original[old_end:] if groups[q] not in exposed_components),
        ),
    }


def freeze(directory: Path) -> dict:
    protocol = verified_protocol()
    ids = {row["query_id"] for row in jsonl(HISTORY / "dev-baseline.jsonl")}
    # Only the mechanical split sees full TRAIN judgments here. Its output exposes
    # IDs and group membership for sampling, never outcomes, documents or grades.
    groups = query_groups(train_qrels())
    cohorts = make_cohorts(ids, groups, protocol["split_plan"])
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / "cohorts.json", cohorts)
    save(directory / "groups.json", groups)
    projection = {
        "frozen_at_utc": datetime.now(UTC).isoformat(),
        "protocol_sha256": sha256(PROTOCOL),
        "query_count": len(ids),
        "cohorts": {
            name: {
                "queries": len(qids),
                "source_components": len({groups[q] for q in qids}),
                "ordered_ids_sha256": fingerprint(qids),
            }
            for name, qids in cohorts.items()
        },
        "total_train_source_components": len(set(groups.values())),
        "development_or_warmup_vs_confirmation_shared_components": len(
            {groups[q] for q in cohorts["development"] + cohorts["warmup"]}
            & {groups[q] for q in cohorts["confirmation"]}
        ),
    }
    save(directory / "cohort-freeze.json", projection)
    return projection


def checked_cohorts(directory: Path) -> tuple[dict, dict]:
    frozen = json.loads((directory / "cohort-freeze.json").read_bytes())
    cohorts = json.loads((directory / "cohorts.json").read_bytes())
    groups = json.loads((directory / "groups.json").read_bytes())
    plan = verified_protocol()["split_plan"]
    if cohorts != make_cohorts(set(groups), groups, plan):
        raise ValueError("registered cohorts changed")
    if frozen["protocol_sha256"] != sha256(PROTOCOL) or any(
        frozen["cohorts"][name]["ordered_ids_sha256"] != fingerprint(qids)
        for name, qids in cohorts.items()
    ):
        raise ValueError("cohort fingerprint differs from freeze")
    return cohorts, groups


def historical_rows(ordered_ids: list[str]) -> dict:
    subset = set(ordered_ids)
    rows = {
        row["query_id"]: row["stages"]
        for row in jsonl(HISTORY / "dev-baseline.jsonl")
        if row["query_id"] in subset
    }
    if set(rows) != subset:
        raise ValueError("incomplete historical lanes")
    return rows


def queries(ordered_ids: list[str]) -> dict[str, str]:
    subset = set(ordered_ids)
    texts = {r["_id"]: r["text"] for r in jsonl(DATA / "queries.jsonl") if r["_id"] in subset}
    if set(texts) != subset:
        raise ValueError("missing registered query text")
    return texts


def snapshot_chunks() -> dict[str, tuple[str, str]]:
    """Return original chunk ID -> (source document ID, text) for all 9189 chunks."""
    snapshot = json.loads(SNAPSHOT.read_bytes())
    protocol = json.loads(PROTOCOL.read_bytes())
    if (snapshot["snapshot_id"], snapshot["snapshot_hash"]) != (
        protocol["snapshot_id"],
        protocol["snapshot_hash"],
    ):
        raise ValueError("lexical corpus is not the paired snapshot")
    by_doc: dict[str, list[str]] = defaultdict(list)
    # scifact_runner.prepare inserts each doc's chunk_sources in original ordinal order.
    for cid, doc in snapshot["chunk_sources"].items():
        by_doc[doc].append(cid)
    verified_revisions = {}
    for pair in jsonl(HISTORY / "pair-token-audit.jsonl"):
        if pair["arm"] == "parent":
            doc, revision = pair["source_document_id"], pair["pair_id"]
            if verified_revisions.setdefault(doc, revision) != revision:
                raise ValueError("conflicting historical revision identities")
    chunks = {}
    for row in jsonl(DATA / "corpus.jsonl"):
        doc = row["_id"]
        text = SourceDocument(doc, row["title"], row["text"]).retrieval_text
        parsed = ParsedDocument((ParsedBlock(text, {"type": "text_range"}),))
        revision = UUID(verified_revisions[doc]) if doc in verified_revisions else UUID(int=0)
        rebuilt = build_deterministic_chunks(
            revision, parsed, chunk_size_chars=1200, chunk_overlap_chars=200
        )
        for chunk, cid in zip(rebuilt, by_doc[doc], strict=True):
            if doc in verified_revisions and chunk.chunk_id != cid:
                raise ValueError("rebuilt chunk order conflicts with original revision IDs")
            chunks[cid] = (doc, chunk.text)
    if len(by_doc) != 5183 or len(chunks) != 9189 or set(chunks) != set(snapshot["chunk_sources"]):
        raise ValueError("lexical corpus lost an original source or chunk")
    return chunks
