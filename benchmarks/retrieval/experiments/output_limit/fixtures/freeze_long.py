"""Create a fixed-seed independent MuSiQue panel before retrieval or generation."""

import argparse
import json
import re
import zipfile
from hashlib import sha256
from pathlib import Path

from benchmarks.retrieval.experiments.output_limit.cases import digest, normalized

SOURCE = Path(
    "data/external/rag-public-20261021/musique/extracted/data/musique_ans_v1.0_train.jsonl"
)
MANIFEST = Path(__file__).with_name("long-manifest.json")
SEED = "agenthub-output-limit-independent-long-20261119-v1"
LEAKED_RALLY = "2hop__135820_89563"
ID = re.compile(rb"\b[234]hop\d*__\d+(?:_\d+)+\b")
QUERY = re.compile(rb'"(?:query|question)"\s*:\s*("(?:[^"\\]|\\.)*")')
SUFFIXES = {".json", ".jsonl", ".py", ".txt", ".md", ".tsv", ".log", ".patch"}
PRIOR = (
    "joint-six-20261118",
    "first-refusal-100-author",
    "bilingual-refusal-100-author",
    "joint-six-review-packets-v1",
    "composite-20261118",
)


def exposure(repo):
    ids = {"2hop__75213_24779"}
    questions = set()

    def collect(raw):
        ids.update(match.decode() for match in ID.findall(raw))
        for token in QUERY.findall(raw):
            try:
                questions.add(normalized(json.loads(token)))
            except (UnicodeDecodeError, ValueError):
                pass

    paths = list((repo / "benchmarks").rglob("*"))
    for dirname in PRIOR:
        paths.extend((repo / ".scratch" / dirname).rglob("*"))
    for p in paths:
        if "output_limit" in p.parts or not p.is_file():
            continue
        if p.suffix.lower() in SUFFIXES:
            collect(p.read_bytes())
        elif p.suffix == ".zip":
            with zipfile.ZipFile(p) as archive:
                for member in archive.infolist():
                    if Path(member.filename).suffix in SUFFIXES:
                        collect(archive.read(member))
    return ids, questions


def build(row, answerable):
    qid = row["id"]
    hops = row["question_decomposition"]
    supports = [h["paragraph_support_idx"] for h in hops]
    docs = []
    for p in row["paragraphs"]:
        if not answerable and p["idx"] == supports[-1]:
            continue
        docs.append(
            {
                "id": f"{qid}::paragraph::{p['idx']}",
                "text": p["title"] + "\n\n" + p["paragraph_text"],
            }
        )
    gold = dict(
        answerable=answerable,
        target=row["answer"] if answerable else None,
        answer_aliases=row["answer_aliases"] if answerable else [],
        support=[f"{qid}::paragraph::{i}" for i in supports] if answerable else [],
        question_decomposition=hops,
    )
    return dict(
        id=qid,
        language="en",
        genre="MuSiQue v1.0 answerable train (two-hop)",
        question=row["question"],
        documents=docs,
        gold=gold,
    )


def eligible(row, answerable):
    hops = row["question_decomposition"]
    if not (
        row["id"].startswith("2hop__")
        and row["answerable"]
        and len(hops) == 2
        and len(row["paragraphs"]) == 20
        and len({h["paragraph_support_idx"] for h in hops}) == 2
    ):
        return False
    paras = {p["idx"]: p for p in row["paragraphs"]}
    aliases = [row["answer"], *row["answer_aliases"]]
    support = paras[hops[-1]["paragraph_support_idx"]]
    if not any(
        normalized(a) in normalized(support["title"] + " " + support["paragraph_text"])
        for a in aliases
    ):
        return False
    if answerable:
        return True
    # Named multiword answers with no surviving alias or distinctive final
    # token make missing-hop abstentions auditable against the entire corpus.
    if len(row["answer"].split()) < 2:
        return False
    kept = [row["question"]] + [
        p["title"] + " " + p["paragraph_text"] for p in paras.values() if p["idx"] != support["idx"]
    ]
    haystack = " ".join(normalized(p) for p in kept)
    return all(
        normalized(a)
        and normalized(a) not in haystack
        and len(normalized(a.split()[-1])) >= 4
        and normalized(a.split()[-1]) not in haystack
        for a in aliases
        if a.strip()
    )


def run(repo, target, register):
    source = repo / SOURCE
    source_sha = sha256(source.read_bytes()).hexdigest()
    rows = {}
    with source.open(encoding="utf8") as stream:
        for line in stream:
            r = json.loads(line)
            if r["id"].startswith("2hop__"):
                rows[r["id"]] = r
    if register:
        ids, questions = exposure(repo)
        for qid in ids & rows.keys():
            questions.add(normalized(rows[qid]["question"]))
        used_hops = {i for qid in ids for i in qid.split("__")[-1].split("_")}
        chosen = []
        for qid in sorted(rows, key=lambda x: sha256((SEED + "|" + x).encode()).digest()):
            row = rows[qid]
            hop_ids = set(qid.split("__")[1].split("_"))
            answerable = len(chosen) % 3 != 2
            if qid in ids or hop_ids & used_hops or normalized(row["question"]) in questions:
                continue
            if not eligible(row, answerable):
                continue
            chosen.append({"id": qid, "answerable": answerable})
            questions.add(normalized(row["question"]))
            used_hops.update(hop_ids)
            if len(chosen) == 21:
                break
        if len(chosen) != 21:
            raise ValueError("Insufficient eligible independent records")
        # Preserve the other twenty registered records. Before generation,
        # manual inspection found an alternate general date in a distractor.
        index = next(i for i, item in enumerate(chosen) if item["id"] == LEAKED_RALLY)
        for qid in sorted(rows, key=lambda x: sha256((SEED + "|" + x).encode()).digest()):
            if (
                qid not in ids
                and not set(qid.split("__")[1].split("_")) & used_hops
                and normalized(rows[qid]["question"]) not in questions
                and eligible(rows[qid], False)
            ):
                chosen[index] = {"id": qid, "answerable": False}
                break
        if chosen[index]["id"] == LEAKED_RALLY:
            raise ValueError("No independent replacement for the leaked negative")
        manifest = dict(
            source=str(SOURCE.as_posix()),
            source_sha256=source_sha,
            seed=SEED,
            exposure_id_count=len(ids),
            exposure_id_set_sha256=digest(sorted(ids)),
            predispatch_exclusion={
                LEAKED_RALLY: "Full Throttle Saloon states the general rally timing"
            },
            selected=chosen,
        )
    else:
        manifest = json.loads(MANIFEST.read_bytes())
        if source_sha != manifest["source_sha256"]:
            raise ValueError("MuSiQue archive hash changed")
    cases = [build(rows[s["id"]], s["answerable"]) for s in manifest["selected"]]
    if register:
        manifest["derived_sha256"] = digest(cases)
        with MANIFEST.open("x", encoding="utf8") as out:
            json.dump(manifest, out, ensure_ascii=False, indent=2)
            out.write("\n")
    if digest(cases) != manifest["derived_sha256"]:
        raise ValueError("Derived corpus changed")
    with target.open("x", encoding="utf8") as out:
        for c in cases:
            out.write(json.dumps(c, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args()
    run(Path.cwd(), args.output, args.register)
