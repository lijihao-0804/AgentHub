"""Replay sentence evidence scoring on the already exposed 16-question reader cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path

from benchmarks.public_rag import evidence_sentence
from benchmarks.public_rag.evidence_sentence import SentenceEvidenceReranker, evidence_question
from benchmarks.public_rag.qa_first_response_cases import PUBLIC, ROOT, TRAIN, paper_text, read
from benchmarks.public_rag.qa_first_response_verify import events_of, reach, window
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.chunking import build_deterministic_chunks, normalize_chunk_text
from packages.knowledge.contracts import RerankCandidate
from packages.knowledge.parser import ParsedBlock, ParsedDocument

HISTORY = PUBLIC / "qa-first-response-20261114/reader"
MODEL = PUBLIC / "reranker-batching-20261114/protocol.json"
TRAIN_SHA = "9458bfe76074a8fa8d1685af02bcc73537aa6d338ad20591dfaff1946bc88bf4"
PROTOCOL_SHA_LF = "d7e18814f84bab06090551bb39d0bc5dc285d52504aa656278dce753e2194766"


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def lf_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def history():
    manifest = HISTORY / "protocol3.json"
    if lf_hash(manifest) != PROTOCOL_SHA_LF or file_hash(TRAIN) != TRAIN_SHA:
        raise ValueError("historical cohort or official QASPER TRAIN source changed")
    cases = [c for c in read(manifest)["cases"] if c["scope"] == "reserve16"]
    primary = read(HISTORY / "B-reserve16.json")
    followup = read(HISTORY / "authenticated-supplement/B-reserve16.json")
    if (primary["snapshot"], primary["snapshot_hash"]) != (
        followup["snapshot"],
        followup["snapshot_hash"],
    ):
        raise ValueError("the authenticated follow-up must have the original snapshot")
    primary_rows = {r["id"]: r for r in primary["rows"]}
    followup_rows = {r["id"]: r for r in followup["rows"]}
    missing = {k for k, r in primary_rows.items() if r["http"] == 401 and not r["events"]}
    if set(primary_rows) != {c["id"] for c in cases} or missing != set(followup_rows):
        raise ValueError("incomplete exposed retrieval cohort")
    receipts = primary_rows | followup_rows
    raw = read(TRAIN)
    sources = {
        ref["source"]
        for row in receipts.values()
        for event in events_of(row, "retrieval")
        for ref in event["candidate"]
    }
    docs = {key: normalize_chunk_text(paper_text(raw[key])) for key in sources}
    return cases, docs, receipts, primary, missing


def reach_full_document(case, docs):
    from uuid import UUID

    if case["negative"]:
        return {}
    key = case["gold_documents"][0]
    parsed = ParsedDocument((ParsedBlock(docs[key], {"type": "text_range"}),))
    chunks = build_deterministic_chunks(
        UUID(int=0), parsed, chunk_size_chars=1200, chunk_overlap_chars=200
    )
    return reach(case, [{"source": key, "ordinal": c.ordinal} for c in chunks], docs)


def same_gold_rank_ablation(c, refs, docs, ranked, scores):
    order = sorted(range(len(refs)), key=lambda i: (-scores[i], i))[:6]
    before = {r["chunk_id"] for r in ranked}
    six = [refs[i] for i in order]
    return dict(
        reach=reach(c, six, docs), common_original=len(before & {r["chunk_id"] for r in six})
    )


def replay(cases, docs, receipts, missing, base, cpu, *, target_id):
    rows, diagnosis = [], None
    method = SentenceEvidenceReranker(base)
    for c in cases:
        row = receipts[c["id"]]
        (trace,) = events_of(row, "retrieval")
        (reader,) = events_of(row, "reader")
        refs = trace["candidate"]
        if len(refs) != 20 or len(reader["chunks"]) != 6 or reader["chunks"] != trace["reranked"]:
            raise ValueError("expected the historical twenty-to-six passage profile")
        chunks = [
            RerankCandidate(r["chunk_id"], window(docs[r["source"]], r["ordinal"])[0]) for r in refs
        ]
        ranked = method.inspect(c["query"], chunks)
        focus_whole = base.rerank(evidence_question(c["query"]), chunks)
        cpu_scores = cpu.rerank(c["query"], chunks)
        # Descending fusion rank is the production tie-break. The trace records
        # the resulting first-stage order, including ties resolved by chunk ID.
        order = sorted(range(len(refs)), key=lambda i: (-ranked[i].score, i))
        gpu_order = sorted(range(len(refs)), key=lambda i: (-ranked[i].whole_score, i))
        cpu_order = sorted(range(len(refs)), key=lambda i: (-cpu_scores[i], i))
        recorded_six = [r["chunk_id"] for r in reader["chunks"]]
        if [chunks[i].chunk_id for i in cpu_order[:6]] != recorded_six:
            raise ValueError(f"historical CPU BGE replay disagrees with the receipt: {c['id']}")
        new_six = [refs[i] for i in order[:6]]
        local = []
        for i, item in enumerate(ranked):
            best = max(item.windows, key=lambda part: part.score)
            ref = refs[i]
            text, start, end = window(docs[ref["source"]], ref["ordinal"])
            content_start = (
                start
                + len(docs[ref["source"]][start:end])
                - len(docs[ref["source"]][start:end].lstrip())
            )
            local.append(
                dict(
                    **ref,
                    pool_rank=i + 1,
                    original_cpu_rank=cpu_order.index(i) + 1,
                    original_cpu_logit=cpu_scores[i],
                    original_cuda_logit=item.whole_score,
                    selected_score=item.score,
                    proposed_rank=order.index(i) + 1,
                    chunk_locator=[start, end],
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    strongest_window=dict(
                        chunk_offsets=[best.start, best.end],
                        source_offsets=[content_start + best.start, content_start + best.end],
                        score=best.score,
                    ),
                )
            )
        stages = {
            "whole_document_chunks": reach_full_document(c, docs),
            "original_top20": reach(c, refs, docs),
            "recorded_top6": reach(c, reader["chunks"], docs),
            "proposed_top6": reach(c, new_six, docs),
        }
        reach_baseline = reader["chunks"]
        ablations = {
            "focus_whole_only": same_gold_rank_ablation(c, refs, docs, reach_baseline, focus_whole),
            "max_two_sentences_only": same_gold_rank_ablation(
                c,
                refs,
                docs,
                reach_baseline,
                [max(w.score for w in item.windows) for item in ranked],
            ),
        }
        # If the first five whole-chunk slots are protected, only one blended
        # chunk outside that prefix can enter. Reuse the already-computed ordering.
        prefix = gpu_order[:5]
        restricted = prefix + [i for i in order if i not in prefix]
        ablations["original_first_five_then_blend"] = {
            "reach": reach(c, [refs[i] for i in restricted[:6]], docs),
            "common_original": len(
                set(recorded_six) & {refs[i]["chunk_id"] for i in restricted[:6]}
            ),
        }
        rows.append(
            dict(
                id=c["id"],
                negative=c["negative"],
                receipt="authenticated-supplement" if c["id"] in missing else "first-response",
                stages=stages,
                ablations=ablations,
                original_six=recorded_six,
                proposed_six=[r["chunk_id"] for r in new_six],
                cpu_original_order_matched=True,
                cuda_original_order_matched=[chunks[i].chunk_id for i in gpu_order[:6]]
                == recorded_six,
                unique_window_pairs=len(
                    {
                        ch.text[w.start : w.end]
                        for ch, item in zip(chunks, ranked, strict=True)
                        for w in item.windows
                    }
                ),
                chunks=local,
            )
        )
        if c["id"] == target_id:
            diagnosis = inspect_target(c, refs, chunks, ranked, local, base, focus_whole)
    return rows, diagnosis


def inspect_target(case, refs, chunks, ranked, local, base, full_focus):
    """Apply gold only after scoring, to identify every native chunk with the official paragraph."""
    query = case["query"]
    focus = evidence_question(query)
    window_texts = [
        [chunk.text[p.start : p.end] for p in item.windows]
        for chunk, item in zip(chunks, ranked, strict=True)
    ]
    unique = list(dict.fromkeys(value for values in window_texts for value in values))
    raw_scores = dict(
        zip(
            unique,
            base.rerank(query, [RerankCandidate(str(i), v) for i, v in enumerate(unique)]),
            strict=True,
        )
    )
    full_title_pair = [max(raw_scores[t] for t in values) for values in window_texts]
    focus_pair = [max(w.score for w in item.windows) for item in ranked]
    variants = {
        "whole_chunk_without_title": full_focus,
        "max_two_sentences_with_title": full_title_pair,
        "max_two_sentences_without_title": focus_pair,
        "half_whole_half_two_sentences_with_title": [
            (item.whole_score + s) / 2 for item, s in zip(ranked, full_title_pair, strict=True)
        ],
    }
    evidence_rows = []
    for i, (chunk, ref) in enumerate(zip(chunks, refs, strict=True)):
        if ref["source"] not in case["gold_documents"] or not any(
            p.casefold() in chunk.text.casefold() for p in case["evidence"]
        ):
            continue
        windows = []
        for sentence in ranked[i].windows:
            source = chunk.text[sentence.start : sentence.end]
            windows.append(
                dict(
                    start=sentence.start,
                    end=sentence.end,
                    focus_logit=sentence.score,
                    full_query_logit=raw_scores[source],
                    contains_answer_span=any(
                        a.casefold() in source.casefold() for a in case["answers"]
                    ),
                )
            )
        evidence_rows.append(
            dict(
                chunk=local[i],
                windows=windows,
                ablations={
                    key: dict(
                        logit=values[i],
                        rank=sorted(range(len(chunks)), key=lambda x: (-values[x], x)).index(i) + 1,
                    )
                    for key, values in variants.items()
                },
            )
        )
    return dict(id=case["id"], query=query, evidence_question=focus, official_support=evidence_rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--target", default="c78f18606524539e4c573481e5bf1e0a242cc33c")
    args = parser.parse_args()
    import torch

    torch.set_num_threads(5)
    spec = read(MODEL)["model"]
    model = args.model or (
        ROOT.parent / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots" / spec["revision"]
    )
    if not all(file_hash(model / k) == v for k, v in spec["file_sha256"].items()):
        raise ValueError("local model/tokenizer differ from the pinned BGE release")
    cases, docs, receipts, original, missing = history()
    base = BgeReranker(model_name=str(model), device="cuda", batch_size=8)
    cpu = BgeReranker(model_name=str(model), device="cpu", batch_size=8)
    base.warm()
    cpu.warm()
    rows, diagnosis = replay(cases, docs, receipts, missing, base, cpu, target_id=args.target)
    if diagnosis is None:
        raise ValueError("target must belong to the already exposed cohort")
    report = make_report(
        rows, diagnosis, original, spec, torch.__version__, torch.cuda.get_device_name()
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf8"
    )


def make_report(rows, diagnosis, original, spec, torch_version, gpu_name):
    positives = [r for r in rows if not r["negative"]]
    summary = {
        stage: {
            metric: sum(r["stages"][stage][metric] for r in positives)
            for metric in ("gold_source", "gold_paragraph", "answer_span")
        }
        for stage in positives[0]["stages"]
    }
    return dict(
        cohort="exposed QASPER TRAIN reserve16, including its four authenticated follow-ups",
        positive_denominator=len(positives),
        negatives=len(rows) - len(positives),
        source_train_sha256=TRAIN_SHA,
        original_protocol_sha256_lf=PROTOCOL_SHA_LF,
        snapshot_hash=original["snapshot_hash"],
        model=dict(
            name=spec["name"],
            revision=spec["revision"],
            batch_size=spec["batch_size"],
            max_length=spec["max_length"],
            file_sha256=spec["file_sha256"],
        ),
        historical_cpu_baseline="production adapter, raw complete query and chunk, FP32",
        common_cuda_scorer="production adapter, FP16",
        torch=torch_version,
        gpu=gpu_name,
        implementation_sha256_lf=lf_hash(Path(evidence_sentence.__file__)),
        cpu_reproduced_in_order=sum(r["cpu_original_order_matched"] for r in rows),
        cuda_reproduced_in_order=sum(r["cuda_original_order_matched"] for r in rows),
        original_top6_overlap_mean=statistics.mean(
            len(set(r["original_six"]) & set(r["proposed_six"])) for r in rows
        ),
        metrics=summary,
        ablations={
            name: {
                metric: sum(r["ablations"][name]["reach"][metric] for r in positives)
                for metric in ("gold_source", "gold_paragraph", "answer_span")
            }
            for name in positives[0]["ablations"]
        },
        rows=rows,
        target=diagnosis,
    )


if __name__ == "__main__":
    main()
