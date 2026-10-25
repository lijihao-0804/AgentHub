"""T1: TAT-DQA table-fact delivery pilot — pypdf baseline vs pdfplumber.

Zero model calls: 30 dev documents (seed 20261026), cell-fact questions whose
answer string exists in the ground-truth page, production chunk+embed+hybrid
pipeline into isolated ctxp-tat-* collections, top-5 delivery credit.
"""

from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from pathlib import Path
from uuid import UUID, uuid4

from packages.core.config.settings import Settings
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.contracts import (
    VectorRecord,
    VectorScope,
)
from packages.knowledge.parser import ParsedBlock, ParsedDocument

HERE = Path(__file__).parent
RUN = HERE / "results" / "table-parse-20261026"
DATA = Path("data/external/tat-dqa")
UUID_NS = UUID("7d2a91c4-3e56-4b8f-9a20-000000000004")
SEED = 20261026
N_DOCS = 30
FINAL_K = 5
_ANSWER_CAP = 24


def load_dev():
    groups = json.loads((DATA / "tatdqa_dataset_dev.json").read_text(encoding="utf-8"))
    return groups


def cell_like(answer: str) -> bool:
    value = answer.strip()
    if not value or len(value) > _ANSWER_CAP:
        return False
    # Cell values are short numeric/percent/currency/date tokens, not prose.
    if re.fullmatch(r"[^ ]*", value):
        return True
    return bool(re.fullmatch(r"[\d,.%\$\€\£\-/ ]{2,24}", value)) and len(value.split()) <= 4


def build_exam(groups):
    """30 docs; per doc, cell-like single-span questions whose answer exists in the page."""
    rng = random.Random(SEED)
    doc_qa: dict[str, list[dict]] = defaultdict(list)
    for group in groups:
        uid = group["doc"]["uid"]
        for question in group["questions"]:
            answers = question.get("answer") or []
            answer_type = question.get("answer_type")
            # TAT-DQA dev labels: span / multi-span / arithmetic / count.
            # Cell-fact questions are single short values: span with one
            # cell-like string, or count (an integer).
            if answer_type == "count":
                answers = [str(answers)]
            elif answer_type == "span" and isinstance(answers, list):
                pass
            else:
                continue
            if len(answers) != 1 or not cell_like(str(answers[0])):
                continue
            doc_qa[uid].append(
                {
                    "question": question["question"],
                    "answer": str(answers[0]).strip(),
                }
            )
    eligible = sorted(uid for uid, items in doc_qa.items() if items)
    chosen = set(rng.sample(eligible, min(N_DOCS, len(eligible))))
    exam = []
    for uid in sorted(chosen):
        pdf = DATA / "docs" / "dev" / f"{uid}.pdf"
        if not pdf.exists():
            continue
        for item in doc_qa[uid]:
            exam.append({"doc_uid": uid, **item})
    return exam


def parse_pypdf(pdf_path: Path) -> ParsedDocument:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path), strict=False)
    blocks = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text:
            blocks.append(ParsedBlock(text, {"type": "page", "page": page_number}))
    return ParsedDocument(tuple(blocks))


def parse_pdfplumber(pdf_path: Path) -> ParsedDocument:
    import pdfplumber

    blocks = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            sections: list[str] = []
            text = page.extract_text() or ""
            if text:
                sections.append(text)
            for table in page.extract_tables():
                rows = []
                for row in table:
                    cells = [(cell or "").replace("\n", " ").strip() for cell in row]
                    if any(cells):
                        rows.append(" | ".join(cells))
                if rows:
                    sections.append("[TABLE]\n" + "\n".join(rows))
            if sections:
                blocks.append(
                    ParsedBlock("\n\n".join(sections), {"type": "page", "page": page_number})
                )
    return ParsedDocument(tuple(blocks))


def _components():
    settings = Settings()
    dense = BgeM3DenseEmbedder(
        model_name=settings.knowledge_embedding_model,
        device=settings.knowledge_embedding_device,
        batch_size=settings.knowledge_embedding_batch_size,
        expected_dimension=settings.knowledge_dense_vector_size,
    )
    sparse = BgeM3SparseEncoder(model_name=settings.knowledge_embedding_model)
    return settings, dense, sparse


def index_arm(arm: str, parse, docs: dict[str, Path]):
    settings, dense, sparse = _components()
    index = QdrantVectorIndex(
        url=settings.qdrant_url,
        collection_name=f"ctxp-tat-{arm}",
        dense_vector_size=settings.knowledge_dense_vector_size,
        timeout_seconds=120,
    )
    index.ensure_collection()
    scope = VectorScope(
        workspace_id="ctxp-tat",
        knowledge_base_id=f"ctxp-tat-{arm}",
        document_revision_ids=("ctxp-frozen",),
    )
    records = []
    texts_by_chunk: dict[str, str] = {}
    for uid, pdf_path in sorted(docs.items()):
        parsed = parse(pdf_path)
        if not parsed.blocks:
            continue
        revision_id = uuid4()
        for candidate in build_deterministic_chunks(
            revision_id,
            parsed,
            chunk_size_chars=1_200,
            chunk_overlap_chars=200,
        ):
            dense_vector = dense.embed_documents((candidate.text,))[0]
            encoding = sparse.encode_documents((candidate.text,))[0]
            chunk_id = str(uuid4())
            records.append(
                VectorRecord(
                    point_id=str(uuid4()),
                    chunk_id=chunk_id,
                    dense=dense_vector,
                    sparse=encoding,
                    payload={
                        "workspace_id": "ctxp-tat",
                        "knowledge_base_id": f"ctxp-tat-{arm}",
                        "document_revision_id": "ctxp-frozen",
                        "chunk_id": chunk_id,
                        "doc_uid": uid,
                    },
                )
            )
            texts_by_chunk[chunk_id] = candidate.text
    index.upsert(tuple(records))
    return index, scope, texts_by_chunk


def run_exam(index, scope, texts_by_chunk, dense, sparse, exam):
    rows = []
    for item in exam:
        dense_vector = dense.embed_query(item["question"])
        dense_hits = index.dense_search(dense_vector, scope=scope, limit=100)
        encoding = sparse.encode_query(item["question"])
        sparse_hits = index.sparse_search(encoding, scope=scope, limit=100)
        fused = fuse_top(dense_hits, sparse_hits)
        delivered = [
            texts_by_chunk[c.chunk_id] for c in fused[:FINAL_K] if c.chunk_id in texts_by_chunk
        ]
        answer = item["answer"]
        hit = any(answer.lower() in text.lower() for text in delivered)
        rows.append({**item, "delivered": hit})
    return rows


def fuse_top(dense_hits, sparse_hits):
    from packages.knowledge.retrieval import fuse_reciprocal_rank

    return fuse_reciprocal_rank(dense_hits, sparse_hits, rrf_k=60, candidate_top_k=100)


def main() -> None:
    groups = load_dev()
    exam_all = build_exam(groups)
    docs = {}
    exam = []
    for item in exam_all:
        pdf = DATA / "docs" / "dev" / f"{item['doc_uid']}.pdf"
        if item["doc_uid"] not in docs and len(docs) < N_DOCS:
            docs[item["doc_uid"]] = pdf
    for item in exam_all:
        if item["doc_uid"] in docs:
            exam.append(item)
    print(f"exam: {len(exam)} questions over {len(docs)} documents", flush=True)

    summary = {"exam_questions": len(exam), "documents": len(docs), "arms": {}}
    for arm, parse in (("baseline", parse_pypdf), ("pilot", parse_pdfplumber)):
        index, scope, texts = index_arm(arm, parse, docs)
        _, dense, sparse = _components()
        rows = run_exam(index, scope, texts, dense, sparse, exam)
        delivered = sum(1 for r in rows if r["delivered"])
        summary["arms"][arm] = {
            "delivered": delivered,
            "rate_pct": round(100 * delivered / len(rows), 1),
        }
        (RUN / f"tat-{arm}-rows.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        index.close()
        print(arm, summary["arms"][arm], flush=True)

    base = summary["arms"]["baseline"]["rate_pct"]
    pilot = summary["arms"]["pilot"]["rate_pct"]
    delta = pilot - base
    # paired bootstrap on per-question hit deltas
    base_payload = json.loads((RUN / "tat-baseline-rows.json").read_text(encoding="utf-8"))
    base_rows = {r["question"]: r["delivered"] for r in base_payload}
    pilot_rows = json.loads((RUN / "tat-pilot-rows.json").read_text(encoding="utf-8"))
    paired = [r["delivered"] - base_rows[r["question"]] for r in pilot_rows]
    boot = random.Random(SEED)
    means = sorted(
        sum(paired[boot.randrange(len(paired))] for _ in paired) / len(paired)
        for _ in range(5_000)
    )
    ci_low = means[int(0.025 * len(means))] * 100
    summary["delta_pp"] = round(delta, 1)
    summary["ci95_low_pp"] = round(ci_low, 1)
    summary["gate"] = {
        "delta_ge_15pp": delta >= 15.0,
        "ci_low_positive": ci_low > 0,
    }
    summary["verdict"] = "PASS" if all(summary["gate"].values()) else "FAIL"
    (RUN / "tat-gate.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
