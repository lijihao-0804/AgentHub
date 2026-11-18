"""Freeze independent question-local MuSiQue cases before ranking or generation."""

import hashlib
import json
import re
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
RAW = ROOT / "data/external/rag-public-20261021/musique/extracted/data"
OUT = ROOT / ".scratch/merge-refill-fresh"
RESULTS = ROOT / "benchmarks/public_rag/results"
SEED = "agenthub-musique-natural-source-refill-v1"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def clean(text):
    return unicodedata.normalize("NFC", text).strip()


def key(text):
    return " ".join(clean(text).casefold().split())


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def metadata(row):
    hops = {step["id"] for step in row["question_decomposition"]}
    evidence = {key(p["paragraph_text"]) for p in row["paragraphs"] if p["is_supporting"]}
    return hops, evidence, key(row["question"])


def dump(path, obj):
    path.write_bytes((json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode())


def main():
    history = set()
    files = []
    # Includes frozen groups and occurrence-suffixed IDs, conservatively.
    pattern = re.compile(rb"[234]hop\d*__\d+(?:_\d+)+")
    for path in sorted(RESULTS.rglob("*")):
        if path.suffix in {".json", ".jsonl"}:
            found = pattern.findall(path.read_bytes())
            if found:
                history.update(i.decode() for i in found)
                files.append(str(path.relative_to(ROOT).as_posix()))
    used_hops, used_texts, used_q = set(), set(), set()
    matched = set()
    for split in ("train", "dev"):
        for row in rows(RAW / f"musique_ans_v1.0_{split}.jsonl"):
            if row["id"] in history:
                matched.add(row["id"])
                hops, texts, question = metadata(row)
                used_hops.update(hops)
                used_texts.update(texts)
                used_q.add(question)
    infile = RAW / "musique_ans_v1.0_train.jsonl"
    candidates = []
    for row in rows(infile):
        pars = row["paragraphs"]
        if (
            not row["answerable"]
            or not row["id"].startswith("2hop__")
            or len(pars) != 20
            or sum(len(clean(p["paragraph_text"])) > 1200 for p in pars) < 2
            or len({key(p["title"]) for p in pars if p["is_supporting"]}) != 2
        ):
            continue
        candidates.append((sha(SEED + "|" + row["id"]), row))
    selected, portable = [], []
    for _, row in sorted(candidates, key=lambda item: item[0]):
        hops, texts, question = metadata(row)
        if row["id"] in history or hops & used_hops or texts & used_texts or question in used_q:
            continue
        used_hops.update(hops)
        used_texts.update(texts)
        used_q.add(question)
        case_dir = OUT / "sources" / row["id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        spans = []
        for p in row["paragraphs"]:
            content = clean(p["paragraph_text"])
            path = case_dir / f"p{p['idx']:02d}.txt"
            path.write_bytes(content.encode())
            p["source_id"] = f"{row['id']}::paragraph::{p['idx']}"
            p["path"] = path.relative_to(ROOT).as_posix()
            if p["is_supporting"]:
                spans.append(
                    dict(
                        source_id=p["source_id"],
                        idx=p["idx"],
                        start=0,
                        end=len(content),
                        sha256=sha(content),
                    )
                )
        row["gold_spans"] = spans
        selected.append(row)
        portable.append(dict(id=row["id"], question_sha256=sha(question), gold_spans=spans))
        if len(selected) == 24:
            break
    assert len(selected) == 24
    dump(OUT / "matched.json", {"cases": selected})
    manifest = dict(
        seed=SEED,
        raw_sha256=hashlib.file_digest(infile.open("rb"), "sha256").hexdigest(),
        history_files=files,
        history_ids=len(history),
        history_matched=len(matched),
        eligible_before_history=len(candidates),
        cases=portable,
    )
    dump(HERE / "manifest.json", manifest)
    print(len(selected), len(candidates), len(history), len(matched))


if __name__ == "__main__":
    main()
