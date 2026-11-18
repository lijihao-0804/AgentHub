"""Reconstruct 100 isolated native-language documents from pinned public source bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote
from urllib.request import urlopen

from benchmarks.retrieval.experiments.first_refusal_100.dataset import (
    canonical_digest as canonical_digest,
)
from benchmarks.retrieval.experiments.first_refusal_100.dataset import digest as digest

from .scoring import score_case as score_case

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
OUTPUT = ROOT / ".scratch/bilingual-refusal-100-docs"
MU_DIR = ROOT / "data/external/rag-public-20261021/musique/extracted/data"
CMRC_REV = "c0eb1b6ba219847457e6af3180da722bbeb656af"
CMRC_DIR = ROOT / "data/external" / ("cmrc2018-" + CMRC_REV[:7])
CMRC_URL = f"https://raw.githubusercontent.com/ymcui/cmrc2018/{CMRC_REV}/squad-style-data/"
CMRC_PINS = {
    "train": "5497aa2f81908e31d6b0e27d99b1f90ab63a8f58fa92fffe5d17cf62eba0c212",
    "dev": "e9ff74231f05c230c6fa88b84441ee334d97234cbb610991cd94b82db00c7f1f",
    "trial": "f3fae95b57da8e03afb2b57467dd221417060ef4d82db13bf22fc88589f3a6f3",
}
SOURCES = {
    "musique": (
        MU_DIR / "musique_ans_v1.0_train.jsonl",
        "83a75b1e11e4e9bb8f8308e72ac40ca617ae4431b3a0d955b61cab259248490a",
    ),
    "cmrc2018": (CMRC_DIR / "cmrc2018_train.json", CMRC_PINS["train"]),
}


def fetch_cmrc() -> None:
    """Fetch only the three registered public files. Never replace a conflicting cache."""
    CMRC_DIR.mkdir(parents=True, exist_ok=True)
    for split, expected in CMRC_PINS.items():
        filename = f"cmrc2018_{split}.json"
        target = CMRC_DIR / filename
        if target.exists():
            if digest(target.read_bytes()) != expected:
                raise ValueError(f"Unexpected CMRC cache contents: {target}")
            continue
        with urlopen(CMRC_URL + filename, timeout=60) as response:
            raw = response.read()
        if digest(raw) != expected:
            raise ValueError(f"CMRC download digest mismatch: {filename}")
        with TemporaryDirectory(dir=CMRC_DIR) as temporary:
            staged = Path(temporary) / filename
            staged.write_bytes(raw)
            try:
                os.link(staged, target)
            except FileExistsError:
                if digest(target.read_bytes()) != expected:
                    raise ValueError(
                        f"Unexpected concurrent CMRC cache contents: {target}"
                    ) from None


def load_cases() -> list[dict]:
    return json.loads((HERE / "cases.json").read_bytes())["cases"]


@cache
def load_documents() -> dict[str, dict]:
    cases = load_cases()
    for path, expected in SOURCES.values():
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"Unexpected public corpus file: {path}")
    documents = {}

    def add(case, title, body):
        raw = f"{title}\n\n{body}\n"
        sha = digest(raw.encode())
        if case["document_sha256"] != sha or case["doc_id"] in documents:
            raise ValueError(f"Document digest or identity mismatch: {case['id']}")
        slug = quote(title.replace(" ", "_"), safe="()_")
        documents[case["doc_id"]] = {
            "id": case["doc_id"],
            "title": title,
            "text": raw,
            "source": f"https://{case['language']}.wikipedia.org/wiki/{slug}",
            "sha256": sha,
            "media_type": "text/plain",
        }

    lines = {c["locator"]["line"]: c for c in cases if c["source_kind"] == "musique"}
    with SOURCES["musique"][0].open(encoding="utf8") as src:
        for index, line in enumerate(src, 1):
            if index not in lines:
                continue
            case = lines[index]
            row = json.loads(line)
            loc = case["locator"]
            para = next(p for p in row["paragraphs"] if p["idx"] == loc["paragraph"])
            if row["id"] != loc["record"] or para["is_supporting"]:
                raise ValueError(f"Unexpected MuSiQue record or annotated evidence: {case['id']}")
            add(case, para["title"], para["paragraph_text"])
    articles = {a["id"]: a for a in json.loads(SOURCES["cmrc2018"][0].read_bytes())["data"]}
    for case in cases:
        if case["source_kind"] == "cmrc2018":
            loc = case["locator"]
            article = articles[loc["article"]]
            passage = next(p for p in article["paragraphs"] if p["id"] == loc["passage"])
            add(case, article["title"], passage["context"])
    if len(documents) != len(cases):
        raise ValueError("A case lacks its own complete bound passage")
    return dict(sorted(documents.items()))


def materialize() -> dict:
    """Only the per-document .txt files are eligible for ingestion into the QA scope."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    entries = []
    for doc in load_documents().values():
        path = OUTPUT / (doc["id"] + ".txt")
        path.write_bytes(doc["text"].encode())
        entries.append(
            {
                **{k: v for k, v in doc.items() if k != "text"},
                "path": path.relative_to(ROOT).as_posix(),
            }
        )
    manifest = {"documents": entries, "cases": load_cases()}
    (OUTPUT / "manifest.json").write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch-cmrc", action="store_true", help="Fetch missing pinned public files"
    )
    if parser.parse_args().fetch_cmrc:
        fetch_cmrc()
    manifest = materialize()
    print(
        f"{len(manifest['cases'])} questions and {len(manifest['documents'])} documents in {OUTPUT}"
    )
