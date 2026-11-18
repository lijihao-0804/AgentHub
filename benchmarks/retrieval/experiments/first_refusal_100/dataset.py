"""Rebuild the frozen single-document QA study from public, checksum-pinned local caches."""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path
from urllib.parse import quote

from .scoring import score_case as score_case

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
OUTPUT = ROOT / ".scratch/first-refusal-100-docs"
PUBLIC = ROOT / "data/external/rag-public-20261021"
SOURCES = {
    "musique": (
        PUBLIC / "musique/extracted/data/musique_ans_v1.0_train.jsonl",
        "83a75b1e11e4e9bb8f8308e72ac40ca617ae4431b3a0d955b61cab259248490a",
    ),
    "qasper": (
        PUBLIC / "qasper/extracted/qasper-train-v0.3.json",
        "9458bfe76074a8fa8d1685af02bcc73537aa6d338ad20591dfaff1946bc88bf4",
    ),
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_digest(value: object) -> str:
    return digest(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def load_cases() -> list[dict]:
    return json.loads((HERE / "cases.json").read_bytes())["cases"]


@cache
def load_documents() -> dict[str, dict]:
    """The only bytes eligible for upload are the returned documents' UTF-8 `text`."""
    cases = load_cases()
    for path, expected in SOURCES.values():
        with path.open("rb") as src:
            if hashlib.file_digest(src, "sha256").hexdigest() != expected:
                raise ValueError(f"Unexpected cached data: {path.name}")
    by_line = {c["locator"]["line"]: c for c in cases if c["source_kind"] == "musique"}
    papers = json.loads(SOURCES["qasper"][0].read_bytes())
    documents = {}

    def add(case, title, body, source):
        text = title + "\n\n" + body + "\n"
        sha = digest(text.encode())
        if sha != case["document_sha256"]:
            raise ValueError("The source document has changed: " + case["id"])
        documents[case["doc_id"]] = {
            "id": case["doc_id"],
            "title": title,
            "text": text,
            "source": source,
            "sha256": sha,
            "media_type": "text/plain",
        }

    with SOURCES["musique"][0].open(encoding="utf8") as src:
        for line_num, line in enumerate(src, 1):
            if line_num not in by_line:
                continue
            case = by_line[line_num]
            loc = case["locator"]
            row = json.loads(line)
            para = next(p for p in row["paragraphs"] if p["idx"] == loc["paragraph"])
            if row["id"] != loc["record"]:
                raise ValueError("MuSiQue record identity has changed")
            title = para["title"]
            url = "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="()_")
            add(case, title, para["paragraph_text"], url)
    for case in cases:
        if case["source_kind"] == "qasper":
            pid = case["locator"]["paper"]
            row = papers[pid]
            add(case, row["title"], row["abstract"], "https://arxiv.org/abs/" + pid)
    if len(documents) != len(cases):
        raise ValueError("A source document is missing or shared between unrelated cases")
    return dict(sorted(documents.items()))


def materialize() -> dict:
    """Produce a local-only upload manifest. Gold fields are never part of the document."""
    OUTPUT.mkdir(exist_ok=True, parents=True)
    cases = load_cases()
    documents = []
    for doc_id, doc in load_documents().items():
        path = OUTPUT / (doc_id + ".txt")
        path.write_bytes(doc["text"].encode())
        public = {key: val for key, val in doc.items() if key != "text"}
        documents.append({**public, "path": path.relative_to(ROOT).as_posix()})
    manifest = {"documents": documents, "cases": cases}
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    (OUTPUT / "manifest.json").write_bytes(encoded)
    return manifest


if __name__ == "__main__":
    result = materialize()
    print(f"{len(result['cases'])} cases, {len(result['documents'])} documents: {OUTPUT}")
