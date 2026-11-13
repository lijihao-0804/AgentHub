"""Trace the 13 published SciFact TRAIN candidate-pool misses against frozen evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from collections import Counter
from pathlib import Path

import numpy as np
from scipy import sparse

from benchmarks.public_rag.reranker_batch_benchmark import (
    PROTOCOL,
    ROOT,
    checked_file,
    records,
    save,
    sha256,
)
from benchmarks.public_rag.scifact_recall_index import MODEL, MODEL_SHA256, frozen_source
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.chunking import normalize_chunk_text
from packages.knowledge.contracts import VectorSearchHit
from packages.knowledge.retrieval import fuse_reciprocal_rank

ARCHIVE_SHA = "11c621288d41ac144d29b13b0f8503b3820b7d6e8b1f6ff24dff335c196d76be"
ARCHIVE_URL = "https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz"
SUPERSET = ROOT / "benchmarks/public_rag/results/budget-diversity-20261022"
SUPERSET_HASHES = {
    "screen-superset.jsonl": "c68ea86a0f35f9ae0eb347f9d0f43d465420d978fd652a4dc22ed14c7f4a0047",
    "screen-superset.meta.json": "aec7a65019b1e6be421a4359ee703362d57ebdc9d7724d3060d3a18a9bb3f27a",
}


def ranking(scores, identities, *, positive=False):
    """Use stable logical IDs to break exact-score ties in the offline reconstruction."""
    order = np.lexsort((identities, -scores)).tolist()
    if positive:
        return [i for i in order if scores[i] > 0]
    return order


def vector_hit(chunk_id):
    # RRF consumes only rank and logical chunk ID. All these source hits are already ordered.
    return VectorSearchHit(chunk_id, 1.0, {"chunk_id": chunk_id})


def fuse(dense, lexical):
    return [
        item.chunk_id
        for item in fuse_reciprocal_rank(
            tuple(vector_hit(x) for x in dense[:30]),
            tuple(vector_hit(x) for x in lexical[:30]),
            rrf_k=60,
            candidate_top_k=60,
        )
    ]


def normalized_vectors(values):
    norm = np.linalg.norm(values, axis=1, keepdims=True)
    if not np.all(np.isfinite(norm) & (norm > 0)):
        raise ValueError("invalid dense vectors")
    return values / norm


def idf_weights(matrix):
    df = np.asarray(matrix.getnnz(axis=0)).ravel()
    return np.log1p((matrix.shape[0] - df + 0.5) / (df + 0.5)).astype(np.float32)


def sparse_scores(matrix, idf, encoding):
    indices = np.array(encoding.indices, dtype=np.int32)
    weights = np.asarray(encoding.values, dtype=np.float32) * idf[indices]
    return np.asarray(matrix[:, indices] @ weights).ravel()


def ranks(ids):
    return {value: position for position, value in enumerate(ids, start=1)}


def document_ranks(ids, chunk_sources):
    return ranks(dict.fromkeys(chunk_sources[i] for i in ids))


def best_rank(chunk_ids, positions):
    return min((positions[i] for i in chunk_ids if i in positions), default=None)


def replacement_rank(score, other_scores):
    """A rank range for a single full article competing with unchanged non-gold chunks."""
    return [
        int(np.count_nonzero(other_scores > score)) + 1,
        int(np.count_nonzero(other_scores >= score)) + 1,
    ]


def sentence_ranges(document, official):
    """Align official sentence tokens to the stored parent, retaining BEIR offsets."""
    title = normalize_chunk_text(document.title)
    if title != normalize_chunk_text(official["title"]):
        raise ValueError("upstream title differs from the indexed document")
    text = normalize_chunk_text(document.retrieval_text)
    prefix = title + "\n\n"
    if not text.startswith(prefix):
        raise ValueError("unexpected title separator")
    tokens = list(re.finditer(r"\S+", text[len(prefix) :]))
    offset = 0
    locations = []
    for sentence in official["abstract"]:
        words = normalize_chunk_text(sentence).split()
        end = offset + len(words)
        aligned = tokens[offset:end]
        if not words or [m.group() for m in aligned] != words:
            raise ValueError("official sentence could not be aligned to the indexed abstract")
        locations.append([len(prefix) + aligned[0].start(), len(prefix) + aligned[-1].end()])
        offset = end
    if offset != len(tokens):
        raise ValueError("official abstract did not cover all indexed tokens")
    return locations


def rationale_coverage(groups, locations, windows):
    result = []
    for group in groups:
        sentences = group["sentences"]
        if not sentences or any(not 0 <= i < len(locations) for i in sentences):
            raise ValueError("invalid official rationale")
        spans = [locations[i] for i in sentences]
        covered_by = [
            [w["ordinal"] for w in windows if w["char_start"] <= a and b <= w["char_end"]]
            for a, b in spans
        ]
        same = set(covered_by[0]).intersection(*covered_by[1:])
        result.append(
            {
                "label": group["label"],
                "sentences_zero_based": sentences,
                "ranges": spans,
                "containing_chunks_per_sentence": covered_by,
                "chunks_containing_entire_set": sorted(same),
            }
        )
    return result


def archive_records(path):
    if sha256(path) != ARCHIVE_SHA:
        raise ValueError("official SciFact archive digest mismatch")
    with tarfile.open(path, "r:gz") as archive:
        # Read only TRAIN labels and the shared corpus.
        claims = {
            str(row["id"]): row
            for line in archive.extractfile("data/claims_train.jsonl")
            if (row := json.loads(line))
        }
        documents = {
            str(row["doc_id"]): row
            for line in archive.extractfile("data/corpus.jsonl")
            if (row := json.loads(line))
        }
    return claims, documents


def historical_stages(protocol):
    frozen = protocol["input_sha256"]
    directory = ROOT / protocol["historical_directory"]
    baseline_meta = json.loads(
        checked_file(directory, "dev-baseline.meta.json", frozen).read_bytes()
    )
    superset_meta = json.loads(
        checked_file(SUPERSET, "screen-superset.meta.json", SUPERSET_HASHES).read_bytes()
    )
    for meta in (baseline_meta, superset_meta):
        if (meta["split"], meta["snapshot_id"]) != ("train", protocol["snapshot_id"]):
            raise ValueError("historical retrieval was not on the frozen TRAIN snapshot")
    base = {
        r["query_id"]: r["stages"]
        for r in records(checked_file(directory, "dev-baseline.jsonl", frozen))
    }
    alternate = {
        r["query_id"]: r["stages"]
        for r in records(checked_file(SUPERSET, "screen-superset.jsonl", SUPERSET_HASHES))
    }
    if len(base) != protocol["source_query_count"] or set(base) != set(alternate):
        raise ValueError("incomplete historical TRAIN capture")
    return base, alternate


def historical_ranks(stages, alternate):
    for name in ("dense", "sparse"):
        if stages[name]["ranking"] != alternate[name]["ranking"]:
            raise ValueError("exact branch data differ between independent historical captures")
    ids = {k: [x["chunk_id"] for x in v["ranking"]] for k, v in stages.items()}
    union = fuse(ids["dense"], ids["sparse"])
    if union[:20] != ids["fusion"] or union[:20] != ids["selection"]:
        raise ValueError("reconstructed historical RRF differs from the original top 20")
    if union != [h["chunk_id"] for h in alternate["selection"]["ranking"]]:
        raise ValueError("union differs from independently scored candidates")
    reranked = [h["chunk_id"] for h in alternate["rerank"]["ranking"]]
    if set(reranked) != set(union) or len(reranked) != len(union):
        raise ValueError("incomplete independent cross-encoder capture")
    return {
        "dense": ranks(ids["dense"]),
        "sparse": ranks(ids["sparse"]),
        "rrf": ranks(union),
        "rerank": ranks(reranked),
    }


def verify_artifacts(cache: Path):
    for name in ("chunks-ready.json", "complete.json"):
        manifest = json.loads((cache / name).read_bytes())
        if name == "chunks-ready.json" and manifest["model_files_sha256"] != MODEL_SHA256:
            raise ValueError("corpus vectors use a different model")
        for file, expected in manifest["sha256"].items():
            if sha256(cache / file) != expected:
                raise ValueError(f"reconstructed artifact digest mismatch: {file}")


def run(cache: Path, archive: Path, output: Path, model: Path):
    protocol = json.loads(PROTOCOL.read_bytes())
    source = frozen_source(protocol)
    base, alternate = historical_stages(protocol)
    if set(base) != set(source.queries):
        raise ValueError("original query IDs differ from TRAIN")
    ids = sorted(
        base, key=lambda q: hashlib.sha256(f"{protocol['sample']['salt']}:{q}".encode()).digest()
    )
    measured = ids[: protocol["sample"]["measured"]]
    gold = {q: {d for d, grade in source.qrels[q].items() if grade > 0} for q in measured}
    misses = [
        q
        for q in measured
        if not any(h["document_id"] in gold[q] for h in base[q]["selection"]["ranking"])
    ]
    published = json.loads((PROTOCOL.parent / "report.json").read_bytes())
    if len(misses) != published["pool_missing_all_judged_sources"]:
        raise ValueError("original cohort did not reproduce the published miss count")
    verify_artifacts(cache)
    claims, official = archive_records(archive)
    rows = list(records(cache / "manifest.jsonl"))
    chunk_ids = [r["chunk_id"] for r in rows]
    chunk_ids_array = np.array(chunk_ids)
    positions = {v: i for i, v in enumerate(chunk_ids)}
    sources = {r["chunk_id"]: r["document_id"] for r in rows}
    doc_ids = json.loads((cache / "document-ids.json").read_bytes())
    if doc_ids != list(source.corpus) or len(rows) != protocol["corpus_chunks"]:
        raise ValueError("unexpected reconstructed inventory")
    doc_ids_array = np.array(doc_ids)
    query_ids = json.loads((cache / "train-query-ids.json").read_bytes())
    query_vectors = normalized_vectors(np.load(cache / "train-query-dense.npy"))
    chunk_vectors = normalized_vectors(np.load(cache / "dense.npy"))
    parent_vectors = normalized_vectors(np.load(cache / "parent-dense.npy"))
    chunk_lexical = sparse.load_npz(cache / "sparse.npz")
    parent_lexical = sparse.load_npz(cache / "parent-sparse.npz")
    chunk_idf, parent_idf = idf_weights(chunk_lexical), idf_weights(parent_lexical)
    for name in (
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "sentencepiece.bpe.model",
    ):
        checked_file(model, name, MODEL_SHA256)
    encoder = BgeM3SparseEncoder(model_name=str(model))
    tokenizer = encoder._load_tokenizer()
    paper_ids = np.array([r["document_id"] for r in rows])
    result = []
    overlap = Counter()
    identical_order = Counter()
    max_score_errors = dict(dense=0.0, sparse=0.0)
    for q in sorted(misses, key=int):
        claim = claims[q]
        if source.queries[q] != claim["claim"] or gold[q] != {
            str(d) for d in claim["cited_doc_ids"]
        }:
            raise ValueError("BEIR and official TRAIN claim/cited document IDs disagree")
        historical = historical_ranks(base[q], alternate[q])
        # A distinct, un-tuned diagnostic: keep each captured branch's 30
        # hits, collapse documents in first-occurrence order, then apply RRF.
        doc_branches = [
            list(dict.fromkeys(h["document_id"] for h in base[q][k]["ranking"]))
            for k in ("dense", "sparse")
        ]
        historical_doc_fusion = ranks(fuse(*doc_branches))
        query_vector = query_vectors[query_ids.index(q)]
        lexical = encoder.encode_query(claim["claim"])
        c_scores = {
            "dense": chunk_vectors @ query_vector,
            "sparse": sparse_scores(chunk_lexical, chunk_idf, lexical),
        }
        p_scores = {
            "dense": parent_vectors @ query_vector,
            "sparse": sparse_scores(parent_lexical, parent_idf, lexical),
        }
        # This extra sparse score keeps the original 9,189-chunk IDF constants fixed.
        article_original_idf = sparse_scores(parent_lexical, chunk_idf, lexical)
        c_ids, p_ids = {}, {}
        for branch in ("dense", "sparse"):
            c_ids[branch] = [
                chunk_ids[i]
                for i in ranking(c_scores[branch], chunk_ids_array, positive=branch == "sparse")
            ]
            p_ids[branch] = [
                doc_ids[i]
                for i in ranking(p_scores[branch], doc_ids_array, positive=branch == "sparse")
            ]
            top = base[q][branch]["ranking"]
            overlap[branch] += len(set(c_ids[branch][:30]) & {h["chunk_id"] for h in top})
            identical_order[branch] += c_ids[branch][:30] == [h["chunk_id"] for h in top]
            err = max(
                abs(c_scores[branch][positions[h["chunk_id"]]].item() - h["score"]) for h in top
            )
            max_score_errors[branch] = max(max_score_errors[branch], err)
        c_ids["rrf"] = fuse(c_ids["dense"], c_ids["sparse"])
        p_ids["rrf"] = fuse(p_ids["dense"], p_ids["sparse"])
        c_rank = {k: ranks(v) for k, v in c_ids.items()}
        c_doc = {k: document_ranks(v, sources) for k, v in c_ids.items()}
        p_rank = {k: ranks(v) for k, v in p_ids.items()}
        source_rows = []
        for doc in sorted(gold[q], key=int):
            indices = np.flatnonzero(paper_ids == doc)
            windows = [rows[i] for i in indices]
            ids_in_doc = [w["chunk_id"] for w in windows]
            coords = sentence_ranges(source.corpus[doc], official[doc])
            evidence = rationale_coverage(claim["evidence"].get(doc, []), coords, windows)
            units = []
            for w in windows:
                chunk = w["chunk_id"]
                units.append(
                    {
                        "ordinal": w["ordinal"],
                        "char_range": [w["char_start"], w["char_end"]],
                        "historical": {k: v.get(chunk) for k, v in historical.items()},
                        "exact": {k: v.get(chunk) for k, v in c_rank.items()},
                    }
                )
            parent_idx = doc_ids.index(doc)
            non_gold = paper_ids != doc
            source_rows.append(
                {
                    "document_id": doc,
                    "historical_best": {k: best_rank(ids_in_doc, v) for k, v in historical.items()},
                    "document_rrf_on_original_branch_hits": historical_doc_fusion.get(doc),
                    "exact_chunk_best": {k: best_rank(ids_in_doc, v) for k, v in c_rank.items()},
                    "exact_document_first_occurrence": {k: v.get(doc) for k, v in c_doc.items()},
                    "parent_corpus": {k: v.get(doc) for k, v in p_rank.items()},
                    "replace_only_this_article_against_other_chunks": {
                        "dense": replacement_rank(
                            p_scores["dense"][parent_idx], c_scores["dense"][non_gold]
                        ),
                        "sparse": replacement_rank(
                            article_original_idf[parent_idx], c_scores["sparse"][non_gold]
                        ),
                    },
                    "indexed_article_chars": len(
                        normalize_chunk_text(source.corpus[doc].retrieval_text)
                    ),
                    "article_tokens_no_truncation": len(
                        tokenizer.encode(
                            normalize_chunk_text(source.corpus[doc].retrieval_text),
                            truncation=False,
                        )
                    ),
                    "chunks": units,
                    "official_rationales": evidence,
                }
            )
        in_union = any(s["historical_best"]["rrf"] for s in source_rows)
        result.append(
            {
                "query_id": q,
                "observed_loss": "rrf_top20" if in_union else "both_branches_top30",
                "historical_union_size": len(historical["rrf"]),
                "sources": source_rows,
            }
        )
    summary = {
        "measured_train_queries": len(measured),
        "complete_pool_misses": len(result),
        "positive_query_source_pairs_among_misses": sum(len(r["sources"]) for r in result),
        "observed_loss": dict(Counter(r["observed_loss"] for r in result)),
        "queries_with_official_sentence_evidence": sum(
            any(s["official_rationales"] for s in r["sources"]) for r in result
        ),
        "historical_branch_top30_vs_exhaustive_top30": {
            "slots_per_branch": 30 * len(misses),
            "overlap": dict(overlap),
            "queries_with_identical_order_all_30": dict(identical_order),
            "max_absolute_reconstructed_minus_recorded_score_on_historical_top30": max_score_errors,
        },
        "source_archive_sha256": ARCHIVE_SHA,
        "source_url": ARCHIVE_URL,
        "model": json.loads((cache / "chunks-ready.json").read_bytes()),
        "article_and_query_vectors": json.loads((cache / "complete.json").read_bytes()),
    }
    output.mkdir(parents=True, exist_ok=False)
    save(output / "cases.json", result)
    save(output / "summary.json", summary)
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in ("model", "article_and_query_vectors")},
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--official-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, default=MODEL)
    args = parser.parse_args()
    run(args.cache, args.official_archive, args.output, args.model_dir)
