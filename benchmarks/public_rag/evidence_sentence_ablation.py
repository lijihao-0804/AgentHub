"""Replay two ranking ablations on fixed independent pools without calling the QA reader."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from benchmarks.public_rag import evidence_sentence
from benchmarks.public_rag.evidence_retention_live import PLAYGROUND
from benchmarks.public_rag.evidence_sentence import SentenceEvidenceReranker, evidence_question
from benchmarks.public_rag.evidence_sentence_dev import MODEL, TRAIN_SHA, file_hash, lf_hash
from benchmarks.public_rag.qa_first_response_cases import ROOT, TRAIN, paper_text, read
from benchmarks.public_rag.qa_first_response_verify import (
    canon,
    events_of,
    reach,
    require,
    sha,
    window,
)
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.contracts import RerankCandidate

FROZEN_METHOD = "079962e1fe172a3e66b524ddf0a24c247cee07d4609521b95e45f75883494901"
NAMES = (
    "original_whole",
    "focused_whole_only",
    "original_whole_and_full_query_two_sentences",
    "frozen_candidate",
)


def verify_numeric_match(trace, ranked, arm):
    require(len(trace) == len(ranked), "unexpected dry-run rerank input size")
    for observed, computed in zip(trace, ranked, strict=True):
        require(
            observed["chunk_id"] == computed.chunk_id
            and observed["whole_score"] == computed.whole_score,
            f"{arm}: dry-run whole-chunk inputs or logits differ from the offline reconstruction",
        )
        if arm == "B":
            require(
                observed["score"] == computed.score
                and [(w["start"], w["end"], w["score"]) for w in observed["windows"]]
                == [(w.start, w.end, w.score) for w in computed.windows],
                "B: dry-run sentence spans or logits differ from the offline reconstruction",
            )


def load_receipts(directory, case_ids):
    result = {}
    for arm in ("A", "B"):
        aggregate = directory / f"{arm}-independent.json"
        if aggregate.is_file():
            capture = read(aggregate)
            indexed = {r["id"]: r for r in capture["rows"]}
            require(len(indexed) == len(capture["rows"]), "duplicate captured question")
            result[arm] = {key: (capture, indexed[key]) for key in case_ids}
        else:
            by_case = {}
            for key in case_ids:
                capture = read(directory / f"{arm}-{key}.json")
                (row,) = capture["rows"]
                by_case[key] = (capture, row)
            result[arm] = by_case
    return result


def run_one(case, receipts, corpus, base, protocol_sha, method):
    traces, ranks, inputs = {}, {}, {}
    digests, snapshot = {}, None
    for arm in ("A", "B"):
        capture, row = receipts[arm][case["id"]]
        require(
            capture["arm"] == arm
            and capture["endpoint"] == PLAYGROUND
            and capture["protocol_sha256"] == protocol_sha
            and capture["source"]["benchmarks/public_rag/evidence_sentence.py"] == FROZEN_METHOD,
            "wrong experiment protocol, endpoint or arm",
        )
        require(snapshot in (None, capture["snapshot_hash"]), "snapshots differ between arms")
        snapshot = capture["snapshot_hash"]
        require(
            row["id"] == case["id"] and row["http"] == 200 and row["physical_model_requests"] == 0,
            "invalid fixed retrieval request",
        )
        (trace,) = events_of(row, "retrieval")
        (incoming,) = events_of(row, "rerank_scores")
        (reader,) = events_of(row, "reader")
        require(
            incoming["arm"] == arm and trace["reranked"] == reader["chunks"], "selection mismatch"
        )
        require(
            trace["snapshot"] == capture["snapshot"] and len(reader["chunks"]) == 6,
            "wrong dry-run snapshot or visible chunk count",
        )
        traces[arm] = trace["candidate"]
        inputs[arm] = incoming["input_scores"]
        ranks[arm] = [ref["chunk_id"] for ref in reader["chunks"]]
        digests[arm] = sha(canon(row))
    require(traces["A"] == traces["B"] and len(traces["A"]) == 20, "unequal initial pools")

    refs = traces["A"]
    docs = {ref["source"]: paper_text(corpus[ref["source"]]) for ref in refs}
    keys = {r["chunk_id"]: r for r in refs}
    # Re-score in the order the actual adapter received. Fusion order supplies
    # downstream tie breaks; it need not be identical to the adapter batch order.
    ids = [s["chunk_id"] for s in inputs["A"]]
    require(len(set(ids)) == len(ids) and set(ids) == set(keys), "unexpected rerank identities")
    source_chunks = [
        RerankCandidate(cid, window(docs[keys[cid]["source"]], keys[cid]["ordinal"])[0])
        for cid in ids
    ]
    ranked = method.inspect(case["query"], source_chunks)
    verify_numeric_match(inputs["A"], ranked, "A")
    verify_numeric_match(inputs["B"], ranked, "B")
    spans = {
        ch.text[w.start : w.end]: None
        for ch, item in zip(source_chunks, ranked, strict=True)
        for w in item.windows
    }
    probes = [RerankCandidate(str(i), s) for i, s in enumerate(spans)]
    raw_pair_logits = dict(zip(spans, base.rerank(case["query"], probes), strict=True))
    pair = [
        (item.whole_score + max(raw_pair_logits[ch.text[w.start : w.end]] for w in item.windows))
        / 2
        for ch, item in zip(source_chunks, ranked, strict=True)
    ]
    scores = (
        [item.whole_score for item in ranked],
        base.rerank(evidence_question(case["query"]), source_chunks),
        pair,
        [item.score for item in ranked],
    )
    selected, metrics = {}, {}
    for name, values in zip(NAMES, scores, strict=True):
        by_id = dict(zip(ids, values, strict=True))
        ordered = sorted(refs, key=lambda r: -by_id[r["chunk_id"]])
        selected[name] = [r["chunk_id"] for r in ordered[:6]]
        metrics[name] = reach(case, ordered[:6], docs)["gold_paragraph"]
    require(
        selected[NAMES[0]] == ranks["A"] and selected[NAMES[-1]] == ranks["B"],
        f"{case['id']}: rebuilt ordering differs from the actual six shown by the API",
    )
    return dict(
        id=case["id"],
        snapshot_hash=snapshot,
        receipt_row_canonical_sha256=digests,
        pool_sha256=sha(canon(refs)),
        retained=metrics,
        top6=selected,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument(
        "--receipts",
        type=Path,
        required=True,
        help="Published retrieval directory or original per-question retrieval files",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    args = parser.parse_args()
    require(lf_hash(Path(evidence_sentence.__file__)) == FROZEN_METHOD, "candidate changed")
    require(file_hash(TRAIN) == TRAIN_SHA, "official QASPER TRAIN changed")
    protocol_hash = hashlib.sha256(args.protocol.read_bytes()).hexdigest()
    protocol = read(args.protocol)
    positives = [c for c in protocol["cases"] if c["scope"] == "independent" and not c["negative"]]
    require(
        len(positives) == len({c["id"] for c in positives}) == 30,
        "unexpected independent positives",
    )
    spec = read(MODEL)["model"]
    frozen = read(args.protocol.parent / "execution-freeze.json")
    require(
        frozen["protocol_sha256"] == protocol_hash
        and frozen["models"][spec["name"]] == spec["revision"]
        and frozen["device"] == args.device
        and frozen["reranker_max_input_tokens"] == spec["max_length"],
        "source protocol, hardware setting or model differs from the dry run",
    )
    path = (
        args.model
        or ROOT.parent / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots" / spec["revision"]
    )
    require(
        all(file_hash(path / k) == v for k, v in spec["file_sha256"].items()),
        "local BGE adapter weights or tokenizer changed",
    )
    base = BgeReranker(
        model_name=str(path),
        device=args.device,
        batch_size=spec["batch_size"],
        max_length=spec["max_length"],
    )
    method = SentenceEvidenceReranker(base)
    corpus = read(TRAIN)
    receipts = load_receipts(args.receipts, [c["id"] for c in positives])
    rows = [run_one(c, receipts, corpus, base, protocol_hash, method) for c in positives]
    require(
        len({r["snapshot_hash"] for r in rows}) == 1, "independent queries do not share a snapshot"
    )
    summary = {
        arm: {
            "complete_evidence_paragraph_at_6": sum(r["retained"][arm] for r in rows),
            "gained_vs_original": [
                r["id"] for r in rows if r["retained"][arm] and not r["retained"][NAMES[0]]
            ],
            "lost_vs_original": [
                r["id"] for r in rows if not r["retained"][arm] and r["retained"][NAMES[0]]
            ],
        }
        for arm in NAMES
    }
    report = dict(
        denominator=len(rows),
        snapshot_hash=rows[0]["snapshot_hash"],
        protocol_sha256=protocol_hash,
        candidate_sha256_lf=FROZEN_METHOD,
        model_snapshot=spec["revision"],
        device=args.device,
        arbitrary_variant_reselection=False,
        provider_calls=0,
        formulas={
            NAMES[0]: "BGE(original full query, whole source chunk)",
            NAMES[1]: "BGE(question after removing explicit title, whole source chunk)",
            NAMES[2]: "mean(original whole, max BGE(full query, two-sentence window))",
            NAMES[3]: "mean(original whole, max BGE(focused question, two-sentence window))",
        },
        summary=summary,
        rows=rows,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf8")


if __name__ == "__main__":
    main()
