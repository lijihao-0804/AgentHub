"""Join five historical refusals to QASPER source passages and provider input hashes."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from benchmarks.public_rag.qa_first_response_cases import paper_text
from benchmarks.public_rag.qa_first_response_verify import canon, reach, sha, window
from packages.knowledge.citation_qa import _evidence_prompt, _NumberedEvidence
from packages.knowledge.contracts import RetrievedEvidence

ARCHIVE = "e0fe09bbe0c7ff5b674a204606f038524b63065f"
BASE = "benchmarks/public_rag/results/published/refusal-consistency-20261114"
# Adjudicator annotations only. Never provided to any retrieval or ranking code.
AUDIT = {
    "1810.05320": (
        "FastText performs best",
        "ABSENT_FROM_POOL",
        "The target paper's four candidates describe generic word/sub-word matching or "
        "previous research. None identifies the selected algorithm.",
    ),
    "1808.03894": (
        "Another round of LSTM reading then produces the final representations",
        "LOST_AT_SELECTION",
        "The Task and Model passage first describes independent LSTM encoding, then "
        "another round of LSTM reading. This answers the two-LSTM-layer question "
        "without repeating the official answer string. Visible chunk 14 mentions "
        "another BiLSTM, but does not establish the complete first-stage description.",
    ),
    "1701.08118": (
        "there is no German hate speech corpus available for our needs",
        "LOST_AT_SELECTION",
        "The corpus compilation section motivates building a German corpus and "
        "identifies Twitter as its source. The visible passage on annotator agreement "
        "omits this context.",
    ),
    "1705.03261": (
        "The DDI corpus contains thousands of XML files",
        "LOST_AT_SELECTION",
        "Preprocessing gives the dataset unit, approximate count and multiple records "
        "per file. Numbers describing classification performance or an example's "
        "instances do not establish dataset size.",
    ),
    "1910.02334": (
        "weakly labeled into hate or non-hate memes, depending on their source",
        "LOST_AT_SELECTION",
        "The Dataset section describes weak labels assigned by collection source and "
        "the assumption made for the Reddit negatives. The reader received no passage "
        "at all from this paper.",
    ),
}


def archived(name):
    return json.loads(subprocess.check_output(["git", "show", f"{ARCHIVE}:{BASE}/{name}"]))


def audit(path):
    corpus_bytes = path.read_bytes()
    corpus = json.loads(corpus_bytes)
    protocol = archived("protocol.json")
    if hashlib.sha256(corpus_bytes).hexdigest() != protocol["train_sha256"]:
        raise ValueError("source corpus mismatch")
    cases = {c["id"]: c for c in protocol["cases"]}
    review = archived("review.json")["rows"]
    snapshots = archived("snapshots/independent.json")["documents"]
    docs = {pid: paper_text(p) for pid, p in corpus.items()}
    rows = []
    for result in archived("live/A-independent.json")["rows"]:
        case = cases[result["id"]]
        if case["negative"] or review[result["id"]]["baseline"]["verdict"] != "refusal":
            continue
        pid = case["gold_documents"][0]
        phrase, finding, reason = AUDIT[pid]
        target = docs[pid]
        offset = target.index(phrase)
        ev = {e["kind"]: e for e in result["events"]}
        reader = ev["reader"]
        visible = {r["chunk_id"]: r for r in reader["entries"]}
        logits = {r["chunk_id"]: r for r in ev["rerank_scores"]["input_scores"]}
        candidates = []
        numbered = []
        for rank, ref in enumerate(ev["retrieval"]["candidate"], 1):
            text, start, end = window(docs[ref["source"]], ref["ordinal"])
            cid = ref["chunk_id"]
            doc = snapshots[ref["source"]]
            identity = sha(
                canon(
                    {
                        "document_revision_id": doc["revision"],
                        "ordinal": ref["ordinal"],
                        "normalized_content_hash": sha(text),
                    }
                )
            )
            if identity != cid:
                raise ValueError("candidate is not the source chunk")
            loc = {"type": "text_range", "char_start": start, "char_end": end}
            score = logits[cid]
            candidates.append(
                {
                    **ref,
                    "revision": doc["revision"],
                    "locator": loc,
                    "fusion_rank": rank,
                    "whole_score": score["whole_score"],
                    "sentence_score": score["score"],
                    "reviewed_support_in_chunk": ref["source"] == pid and phrase in text,
                    "reader_position": visible.get(cid, {}).get("number"),
                }
            )
            if cid in visible:
                item = visible[cid]
                if item["text"] != text or item["locator"] != loc:
                    raise ValueError("reader/source divergence")
                record = RetrievedEvidence(
                    doc["document_id"],
                    doc["revision"],
                    cid,
                    item["source"],
                    loc,
                    text,
                    0,
                    None,
                    item["lifecycle"],
                )
                numbered.append(_NumberedEvidence(item["number"], record, text))
        prompt = _evidence_prompt(case["query"], tuple(sorted(numbered, key=lambda i: i.id)))
        wire = next(e for e in result["events"] if e["kind"] == "http_wire")
        if sha(prompt) != reader["prompt_sha256"] or sha(prompt) != wire["content_sha256"][1]:
            raise ValueError("initial provider input differs")
        correct = sum(c["reviewed_support_in_chunk"] for c in candidates)
        if bool(correct) != (finding == "LOST_AT_SELECTION"):
            raise ValueError("review annotation drift")
        rows.append(
            {
                "question_id": case["id"],
                "question": case["query"],
                "target_paper": pid,
                "snapshot": ev["retrieval"]["snapshot"],
                "source_fact": {
                    "text": phrase,
                    "paper_char_start": offset,
                    "paper_char_end": offset + len(phrase),
                    "official_evidence": case["evidence"],
                },
                "literal_pool": reach(case, ev["retrieval"]["candidate"], docs),
                "literal_reader": reach(case, reader["chunks"], docs),
                "source_review": finding,
                "rationale": reason,
                "candidate_records": candidates,
                "initial_wire_user_sha256": sha(prompt),
                "reader_entries": reader["entries"],
                "answer": result["answer"],
                "insufficient": result["insufficient"],
                "citations": result["citations"],
                "physical_model_requests_historical": result["physical_model_requests"],
            }
        )
    if len(rows) != len(AUDIT):
        raise ValueError("changed failure cohort")
    return {
        "archive_commit": ARCHIVE,
        "archive_path": BASE,
        "entry": "POST /api/v1/knowledge/query (Citation QA); no tool stage on these calls",
        "new_physical_requests": 0,
        "qasper_train_sha256": protocol["train_sha256"],
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--qasper",
        type=Path,
        default=Path("data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json"),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.write_text(
        json.dumps(audit(args.qasper), ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
