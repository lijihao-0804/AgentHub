"""Read the historical BGE order, bind each text to its snapshot and locate the gold evidence."""

import json
from dataclasses import dataclass

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.candidate_recall_rerank import MODEL_PROTOCOL, verified_inputs
from benchmarks.public_rag.data import load_beir
from benchmarks.public_rag.fiqa_confirmation import _chunks
from benchmarks.public_rag.scifact_recall_forensics import archive_records, sentence_ranges
from benchmarks.public_rag.scifact_recall_index import chunk_manifest, frozen_source
from packages.knowledge.chunking import normalize_chunk_text

ROOT = data.ROOT
FIQA = ROOT / "data/external/rag-fiqa-20261022/extracted/fiqa"
FIQA_STATE = ROOT / ".scratch/rag-fiqa-20261022/fiqa-state.json"
FIQA_RANK = ROOT / "benchmarks/public_rag/results/fiqa-confirmation-20261022/baseline.jsonl"
STUDY = ROOT / ".scratch/candidate-recall-scifact-20261114"
SCIFACT_GOLD = ROOT / ".scratch/scifact-rationale-source-20261114/data.tar.gz"
SCIFACT_RANK = data.HISTORY / "dev-baseline.jsonl"


@dataclass(frozen=True)
class Window:
    chunk: str
    doc: str
    revision: str | None
    start: int
    end: int
    text: str
    locator: dict | None = None


def checked_window(chunk, doc, revision, locator, displayed, texts):
    text = texts[doc]
    lo, hi = locator["char_start"], locator["char_end"]
    original_locator = (
        locator if "type" in locator else {"type": "text_range", "char_start": lo, "char_end": hi}
    )
    if not (0 <= lo < hi <= len(text)):
        raise ValueError("Out-of-bounds archived source span")
    raw = text[lo:hi]
    lo += len(raw) - len(raw.lstrip())
    hi -= len(raw) - len(raw.rstrip())
    if not displayed or text[lo:hi] != displayed:
        raise ValueError("Displayed text differs from the historic source interval")
    return Window(chunk, doc, revision, lo, hi, displayed, original_locator)


def pinned(paths):
    result = {}
    for path, expected in paths.items():
        digest = data.sha256(path)
        if digest != expected:
            raise ValueError(f"Historical input differs: {path.name}")
        result[path.relative_to(ROOT).as_posix()] = digest
    return result


def ordered(stages, windows):
    fused, rerank = stages["fusion"]["ranking"], stages["rerank"]["ranking"]
    keys = [h["chunk_id"] for h in rerank]
    if len(keys) != 20 or len(set(keys)) != 20 or set(keys) != {h["chunk_id"] for h in fused}:
        raise ValueError("Expected the full original twenty-chunk pool")
    result = [windows[k] for k in keys]
    if [w.doc for w in result] != [h["document_id"] for h in rerank]:
        raise ValueError("Archived ranking and indexed sources disagree")
    return result


async def fiqa():
    pins = pinned(
        {
            FIQA_RANK: "190eea5eade1af85bda24c892dfef119fde5c94a151f13ec9681b6e3509280f1",
            FIQA_STATE: "ce2147254e455ec7d9e25f994ad8c1c8a29e9541227f773ecdd094c3bd1a6f2e",
            FIQA
            / "corpus.jsonl": "ff593e4df9933955dc3af83be0c3fa28ac7465f627e08c2e53593e734d506517",
            FIQA
            / "queries.jsonl": "eede1e61d4a0188940239b53ebc2da91f577a6a34679c812d1eb9090c29877bc",
            FIQA
            / "qrels/test.tsv": "6adc2a640dcdd22bb8b3858f89107adef2a7c3db20a63550dfa7a0f71e379e44",
        }
    )
    src = load_beir(FIQA, "test")
    state = json.loads(FIQA_STATE.read_bytes())
    texts = {k: normalize_chunk_text(v.retrieval_text) for k, v in src.corpus.items()}
    rows = await _chunks(state, src.corpus)
    windows = {
        k: checked_window(
            k, state["chunk_sources"][k], str(r.document_revision_id), r.locator, r.text, texts
        )
        for k, r in rows.items()
    }
    ranks = {r["query_id"]: ordered(r["stages"], windows) for r in data.jsonl(FIQA_RANK)}
    if len(ranks) != 648 or ranks.keys() != src.queries.keys():
        raise ValueError("Incomplete FiQA TEST population")
    return (
        src,
        ranks,
        texts,
        {},
        {
            "snapshot_hash": state["snapshot_hash"],
            "input_sha256": pins,
            "distinct_indexed_chunks": len(windows),
            "order": "Archived BGE TEST order, same 20 unique chunks as original fusion.",
        },
    )


def scifact():
    pins = pinned(
        {
            SCIFACT_RANK: "cf391506ecdc72dc2d65fd0e8a943b078a1386ae2427603b61b4cd294822a4f9",
            SCIFACT_GOLD: "11c621288d41ac144d29b13b0f8503b3820b7d6e8b1f6ff24dff335c196d76be",
            MODEL_PROTOCOL: "ebf3105ad9ace94923b190f0a5b92a9b015379a65e6f2c6d23714ef648afa9db",
            data.PROTOCOL: "c4c024b0876db23a3fa068c33cd13f2a9f7e3de8f568567d50954bc61b9baef9",
        }
    )
    _, cohorts, _, candidates, _, _ = verified_inputs(STUDY)
    qids = cohorts["confirmation"]
    model = json.loads(MODEL_PROTOCOL.read_bytes())
    src = frozen_source(model)
    texts = {k: normalize_chunk_text(v.retrieval_text) for k, v in src.corpus.items()}
    windows = {
        h["chunk_id"]: checked_window(
            h["chunk_id"], h["document_id"], h["revision_id"], h, h["text"], texts
        )
        for h in chunk_manifest(src, data.SNAPSHOT, model)
    }
    history = data.historical_rows(qids)
    ranks = {q: ordered(history[q], windows) for q in qids}
    claims, docs = archive_records(SCIFACT_GOLD)
    requirements = {}
    for q in qids:
        if {h["chunk_id"] for h in candidates[q]["baseline"]} != {w.chunk for w in ranks[q]}:
            raise ValueError("Verified candidate cache and original ranking differ")
        groups = {}
        for doc, alternatives in claims[q]["evidence"].items():
            if not alternatives:
                continue
            if src.qrels[q].get(doc, 0) <= 0:
                raise ValueError("Sentence annotator and qrels disagree about a source")
            positions = sentence_ranges(src.corpus[doc], docs[doc])
            for group in alternatives:
                ids = tuple(sorted(set(group["sentences"])))
                if not ids or not all(0 <= i < len(positions) for i in ids):
                    raise ValueError("Invalid official evidence set")
                groups[(doc, group["label"], ids)] = [positions[i] for i in ids]
        requirements[q] = groups
    if len(qids) != 553 or sum(len({key[0] for key in gs}) for gs in requirements.values()) != 379:
        raise ValueError("Incorrect SciFact query or annotated source-pair population")
    return (
        src,
        ranks,
        texts,
        requirements,
        {
            "snapshot_hash": model["snapshot_hash"],
            "input_sha256": pins,
            "distinct_indexed_chunks": len(windows),
            "ordered_query_ids_sha256": data.fingerprint(qids),
            "cohort": "previously exposed TRAIN confirmation",
            "order": "Archived BGE order and original twenty chunks verified against the cache.",
        },
    )
