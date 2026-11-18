"""Freeze independent MuSiQue pairs and rebuild their local, question-scoped sources."""

import argparse
import hashlib
import json
import re
import subprocess
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
RAW = ROOT / "data/external/rag-public-20261021/musique/extracted/data"
OUT = ROOT / ".scratch/joint-six-20261118"
HISTORICAL = "b7cdb0a5cdb4f19cab215e27591ff1ef9d48261b"
ARCHIVE = "benchmarks/public_rag/experiments/musique_source_refill"
SEED = "agenthub-joint-six-independent-v1-20261118"
CASE_PATTERN = re.compile(rb"[234]hop\d*__\d+(?:_\d+)+")
TARGET = {"primary_long": 24, "primary_ordinary": 24, "missing_bridge": 8, "missing_final": 8}
CONTROL = {"missing_bridge", "missing_final"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def as_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def write_equal(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Existing freeze material differs: {path}")
        return
    with path.open("xb") as stream:
        stream.write(data)


def raw_rows(path):
    with path.open("rb") as stream:
        for line_no, line in enumerate(stream, 1):
            yield line_no, json.loads(line), line.rstrip(b"\r\n")


def clean(value):
    return unicodedata.normalize("NFC", value).strip()


def key(value):
    folded = unicodedata.normalize("NFKD", value.casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", folded))


def archive(name):
    raw = subprocess.check_output(["git", "show", f"{HISTORICAL}:{ARCHIVE}/{name}"], cwd=ROOT)
    return json.loads(raw)


def study_history():
    old = archive("manifest.json")
    seen = set()
    files = {}
    questions, protected_titles, protected_texts = set(), set(), set()

    def read_history(path):
        data = path.read_bytes()
        files[path.relative_to(ROOT).as_posix()] = sha(data)
        seen.update(x.decode() for x in CASE_PATTERN.findall(data))
        return json.loads(data) if path.suffix == ".json" else None

    for filename in old["history_files"]:
        read_history(ROOT / filename)
    if len(seen) != old["history_ids"]:
        raise ValueError("Previous exposure inventory has changed")
    old_ids = {case["id"] for case in old["cases"]}
    prior = ROOT / ".scratch/merge-refill-fresh/matched.json"
    if file_hash(prior) != "52e871adf5176d1f93f7c5162b34661da0b4be606b93155543215c83ebc22e55":
        raise ValueError("Previous 24-question registration has changed")
    for case in read_history(prior)["cases"]:
        questions.add(key(case["question"]))
        for p in case["paragraphs"]:
            protected_titles.add(key(p["title"]))
            protected_texts.add(key(p["paragraph_text"]))
    for name in ("first-refusal-100", "bilingual-refusal-100"):
        manifest = read_history(ROOT / f".scratch/{name}-docs/manifest.json")
        if len(manifest["cases"]) != 100:
            raise ValueError("Protected controls must contain 100 questions per suite")
        questions.update(key(c["question"]) for c in manifest["cases"])
        for doc in manifest["documents"]:
            data = (ROOT / doc["path"]).read_bytes()
            if sha(data) != doc["sha256"]:
                raise ValueError("Protected text differs from registered source")
            protected_titles.add(key(doc["title"]))
            protected_texts.add(key(data.decode()))
        # Includes sources considered during historical question authoring.
        options = read_history(ROOT / f".scratch/{name}-author/options.json")
        for group in options.values():
            for doc in group:
                protected_titles.add(key(doc["title"]))
                protected_texts.add(key(doc["text"]))
    read_history(ROOT / ".scratch/first-refusal-100-author/exclusions.json")
    wai = read_history(ROOT / "benchmarks/retrieval/table_header_protocol.json")
    if len(wai["cases"]) != 14:
        raise ValueError("WAI control count differs")
    questions.update(key(c[0]) for c in wai["cases"])
    wai_source = ROOT / ".scratch/rag-table-wai-20261118/irregular.html"
    files[wai_source.relative_to(ROOT).as_posix()] = file_hash(wai_source)
    protected_texts.add(key(wai_source.read_text(encoding="utf8")))

    hops, old_gold, old_any, matched = set(), set(), set(), set()
    # One ID can have paraphrases and distinct answerable/unanswerable contexts.
    # Excluding every matching upstream variant is deliberately conservative.
    for split in ("train", "dev"):
        for variant in ("ans", "full"):
            for _, row, _ in raw_rows(RAW / f"musique_{variant}_v1.0_{split}.jsonl"):
                if row["id"] not in seen:
                    continue
                matched.add(row["id"])
                questions.add(key(row["question"]))
                hops.update(s["id"] for s in row["question_decomposition"])
                for p in row["paragraphs"]:
                    body = key(p["paragraph_text"])
                    old_any.add(body)
                    if p["is_supporting"]:
                        old_gold.add(body)
                    if row["id"] in old_ids:
                        protected_titles.add(key(p["title"]))
                        protected_texts.add(body)
    audit = dict(
        previous_commit=HISTORICAL,
        previous_questions=24,
        protected_controls=214,
        historical_strings=len(seen),
        matched_upstream_ids=len(matched),
        unmatched_id_strings=sorted(seen - matched),
        hop_ids=len(hops),
        normalized_paragraphs=len(old_any),
        normalized_supports=len(old_gold),
        protected_titles=len(protected_titles),
        files=files,
    )
    return (seen, questions, hops, protected_titles, protected_texts, old_gold, old_any), audit


def negative_check(row, group):
    tokens = [" " + key(p["title"] + " " + p["paragraph_text"]) + " " for p in row["paragraphs"]]
    if group == "missing_final":
        values = [key(v) for v in (row["answer"], *row["answer_aliases"])]
        if all(v and not any(f" {v} " in text for text in tokens) for v in values):
            return "terminal_value_absent_from_all_20_titles_and_bodies"
    else:
        first = row["question_decomposition"][0]["question"]
        if " >> " in first:
            subject = key(first.split(" >> ", 1)[0])
            if any(
                len(word) >= 4 and all(f" {word} " not in text for text in tokens)
                for word in subject.split()
            ):
                return "at_least_one_bridge_subject_word_absent_from_all_20_titles_and_bodies"
    return None


def group_for(row):
    pars = row["paragraphs"]
    steps = row["question_decomposition"]
    if (
        not row["id"].startswith("2hop__")
        or len(steps) != 2
        or len(pars) != 20
        or {p["idx"] for p in pars} != set(range(20))
    ):
        return None
    locations = [s["paragraph_support_idx"] for s in steps]
    if {p["idx"] for p in pars if p["is_supporting"]} != {i for i in locations if i is not None}:
        return None
    if row["answerable"]:
        if len({key(p["title"]) for p in pars if p["is_supporting"]}) != 2:
            return None
        long_paragraphs = sum(len(clean(p["paragraph_text"])) > 1200 for p in pars)
        return "primary_long" if long_paragraphs >= 2 else "primary_ordinary"
    if (locations[0] is None) == (locations[1] is None):
        return None
    return "missing_bridge" if locations[0] is None else "missing_final"


def candidates(exposure):
    seen, questions, hops, titles, texts, old_gold, old_any = exposure
    groups = defaultdict(list)
    for variant in ("ans", "full"):
        name = f"musique_{variant}_v1.0_train.jsonl"
        for line_no, row, raw in raw_rows(RAW / name):
            if (variant == "full") == row["answerable"]:
                continue
            group = group_for(row)
            if not group or row["id"] in seen or key(row["question"]) in questions:
                continue
            if any(s["id"] in hops for s in row["question_decomposition"]):
                continue
            pars = row["paragraphs"]
            if any(key(p["title"]) in titles for p in pars):
                continue
            bodies = {key(p["paragraph_text"]) for p in pars}
            if not bodies.isdisjoint(texts) or not bodies.isdisjoint(old_gold):
                continue
            if any(key(p["paragraph_text"]) in old_any for p in pars if p["is_supporting"]):
                continue
            negative = negative_check(row, group) if group in CONTROL else None
            if group in CONTROL and not negative:
                continue
            sort_key = sha(f"{SEED}|{group}|{row['id']}|{line_no}".encode())
            groups[group].append((sort_key, name, line_no, sha(raw), negative, row))
    return {group: sorted(items) for group, items in groups.items()}


def register():
    exposure, audit = study_history()
    pools = candidates(exposure)
    titles, texts, ids, questions, hops = set(), set(), set(), set(), set()
    selected = []
    for group in ("missing_bridge", "missing_final", "primary_long", "primary_ordinary", "smoke"):
        target = 1 if group == "smoke" else TARGET[group]
        pool = pools["primary_ordinary" if group == "smoke" else group]
        taken = 0
        for _, name, line_no, raw_sha, negative, row in pool:
            q = key(row["question"])
            source_titles = {key(p["title"]) for p in row["paragraphs"]}
            source_texts = {key(p["paragraph_text"]) for p in row["paragraphs"]}
            step_ids = {s["id"] for s in row["question_decomposition"]}
            if (
                row["id"] in ids
                or q in questions
                or step_ids & hops
                or source_titles & titles
                or source_texts & texts
            ):
                continue
            ids.add(row["id"])
            questions.add(q)
            hops.update(step_ids)
            titles.update(source_titles)
            texts.update(source_texts)
            selected.append(registered_case(row, group, negative, name, line_no, raw_sha))
            taken += 1
            if taken == target:
                break
        if taken != target:
            raise ValueError(f"Only {taken} independent rows in {group}")
    # Interleave subgroups for dispatch order without looking at model scores.
    selected.sort(key=lambda c: sha(f"{SEED}|order|{c['id']}".encode()))
    return dict(
        schema=1,
        seed=SEED,
        history=audit,
        training_files={n: file_hash(RAW / n) for n in sorted({c["file"] for c in selected})},
        eligible_rows_before_mutual_exclusion={g: len(p) for g, p in pools.items()},
        cohorts=dict(Counter(c["stratum"] for c in selected)),
        cases=selected,
    )


def registered_case(row, group, negative, name, line_no, raw_sha):
    spans = []
    source_rows = []
    for p in row["paragraphs"]:
        body = clean(p["paragraph_text"])
        digest = sha(body.encode())
        source_rows.append([p["idx"], p["title"], digest])
        if p["is_supporting"]:
            spans.append(
                dict(
                    idx=p["idx"],
                    source_id=f"{row['id']}::paragraph::{p['idx']}",
                    start=0,
                    end=len(body),
                    sha256=digest,
                )
            )
    return dict(
        id=row["id"],
        file=name,
        line=line_no,
        row_sha256=raw_sha,
        stratum=group,
        cohort="insufficient" if group in CONTROL else group,
        answerable=row["answerable"],
        lexical_control=negative,
        hops=[s["id"] for s in row["question_decomposition"]],
        gold_in_hop_order=[s["paragraph_support_idx"] for s in row["question_decomposition"]],
        gold_spans=spans,
        answer_and_aliases_sha256=sha(as_bytes([row["answer"], row["answer_aliases"]])),
        question_sha256=sha(clean(row["question"]).encode()),
        ordered_twenty_sources_sha256=sha(as_bytes(source_rows)),
    )


def materialize(manifest):
    cases = {}
    for name, expected in manifest["training_files"].items():
        path = RAW / name
        if file_hash(path) != expected:
            raise ValueError(f"Original data differs: {name}")
        needed = {c["line"]: c for c in manifest["cases"] if c["file"] == name}
        for line_no, row, raw in raw_rows(path):
            if line_no not in needed:
                continue
            reg = needed[line_no]
            checked = registered_case(
                row, reg["stratum"], reg["lexical_control"], name, line_no, sha(raw)
            )
            if checked != reg:
                raise ValueError(f"Registered source or gold differs: {reg['id']}")
            for p in row["paragraphs"]:
                body = clean(p["paragraph_text"])
                dst = OUT / "sources" / row["id"] / f"p{p['idx']:02d}.txt"
                write_equal(dst, body.encode())
                p.update(
                    paragraph_text=body,
                    path=dst.relative_to(ROOT).as_posix(),
                    source_id=f"{row['id']}::paragraph::{p['idx']}",
                )
            row.update(cohort=reg["cohort"], stratum=reg["stratum"], gold_spans=reg["gold_spans"])
            cases[row["id"]] = row
    complete = [cases[c["id"]] for c in manifest["cases"]]
    write_equal(OUT / "matched.json", as_bytes({"cases": complete}))
    return file_hash(OUT / "matched.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", action="store_true", help="recompute selection and exposure")
    args = parser.parse_args()
    path = HERE / "manifest.json"
    if args.register:
        write_equal(path, as_bytes(register()))
    manifest = json.loads(path.read_bytes())
    print(json.dumps(dict(cases=len(manifest["cases"]), matched_sha256=materialize(manifest))))
