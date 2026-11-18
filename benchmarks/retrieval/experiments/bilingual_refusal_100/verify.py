"""Verify independent sources, historical exclusion, bilingual gold and scoring before dispatch."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from functools import lru_cache

from .dataset import (
    CMRC_DIR,
    CMRC_PINS,
    HERE,
    MU_DIR,
    ROOT,
    SOURCES,
    canonical_digest,
    digest,
    load_cases,
    load_documents,
)
from .scoring import HAN, evidence_covers, has_alias, language_match, norm, score_case

PRIOR = HERE.parent / "first_refusal_100"
HISTORICAL_HEAD = "4ce80094b8bbe07da430414085528166fae303db"
PUBLIC_RECEIPTS = ROOT / "benchmarks/public_rag/results"
MUSIQUE_ID = re.compile(rb"\b[2-4]hop\d*__\d+(?:_\d+)+\b")
CMRC_ID = re.compile(rb"\b(?:TRAIN|DEV|TRIAL)_\d+(?:_QUERY_\d+)?\b")
QUESTION = re.compile(
    rb'"(?:question|query|question_text|query_text)"\s*:\s*("(?:[^"\\]|\\.){1,1500}")'
)
FILES = ("cases.json", "dataset.py", "scoring.py", "verify.py", "rubric.md")
MUSIC_PINS = {
    "ans_dev": "15fa63794d18a94ce12411aca6e2327e65b6e83b0b1490efab3f1962e48abf3b",
    "ans_test": "92ee3067957b8ebce885baa71156a7c0c29f3bc72cca04510aef506911dd768f",
    "ans_train": SOURCES["musique"][1],
    "full_dev": "8cab31d56a3a1c4ef491b205a8dab3f1ac9c66e472098c6cf1de4e20294f7a4a",
    "full_test": "01d7b0f752bdc1fc98856d605560e021a89be903baeb9daff11adeb7fac61328",
    "full_train": "b1cd998f7e0e2838d6fda024e4ad1eb0e7fc3edefdadb0bd9b5b10b0907f2034",
}


def lf_digest(path):
    return digest(path.read_bytes().replace(b"\r\n", b"\n"))


def artifact_bindings():
    return {
        **{name: lf_digest(HERE / name) for name in FILES},
        **{f"../first_refusal_100/{n}": lf_digest(PRIOR / n) for n in ("dataset.py", "scoring.py")},
    }


def _history_files():
    listing = subprocess.check_output(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-tree",
            "-zr",
            "--name-only",
            HISTORICAL_HEAD,
            "--",
            "benchmarks",
        ]
    )
    tracked = {ROOT / p.decode("utf8") for p in listing.split(b"\0") if p}
    retained = {p for p in PUBLIC_RECEIPTS.rglob("*") if p.is_file()}
    return sorted(p for p in tracked | retained if p.suffix in {".json", ".jsonl"})


def audit_history() -> dict:
    """Read historical identifiers/text and source splits without access to this study's outputs."""
    documents = load_documents()
    cases = load_cases()
    prior_cases = json.loads((PRIOR / "cases.json").read_bytes())["cases"]
    records = {c["locator"]["record"] for c in prior_cases if c["source_kind"] == "musique"}
    cmrc_ids = set()
    questions = set()
    files = {}
    seen_titles = set()
    seen_body_hashes = set()
    study_titles = {documents[c["doc_id"]]["title"]: c for c in cases}
    related_questions = {c["id"]: set() for c in cases}
    normalize = lru_cache(maxsize=120_000)(norm)
    for path in _history_files():
        data = path.read_bytes()
        files[path.relative_to(ROOT).as_posix()] = digest(data.replace(b"\r\n", b"\n"))
        records.update(s.decode() for s in MUSIQUE_ID.findall(data))
        cmrc_ids.update(s.decode().split("_QUERY_")[0] for s in CMRC_ID.findall(data))
        questions.update(normalize(json.loads(m.group(1))) for m in QUESTION.finditer(data))
    upstream = set()
    for key, expected_hash in MUSIC_PINS.items():
        variant, split = key.split("_")
        path = MU_DIR / f"musique_{variant}_v1.0_{split}.jsonl"
        checksum = hashlib.sha256()
        with path.open("rb") as src:
            for line in src:
                checksum.update(line)
                row = json.loads(line)
                paragraphs = row["paragraphs"]
                if split != "train" or row["id"] in records:
                    seen_titles.update(normalize(p["title"]) for p in paragraphs)
                    seen_body_hashes.update(
                        digest(p["paragraph_text"].encode()) for p in paragraphs
                    )
                if split != "train":
                    continue
                upstream.add(normalize(row["question"]))
                # Other training records can use the same article as official evidence.
                title_by_idx = {p["idx"]: p["title"] for p in paragraphs if p["is_supporting"]}
                for qa in row.get("question_decomposition", []):
                    upstream.add(normalize(qa["question"]))
                    title = title_by_idx.get(qa.get("paragraph_support_idx"))
                    if title in study_titles:
                        related_questions[study_titles[title]["id"]].add(qa["question"])
                        related_questions[study_titles[title]["id"]].add(row["question"])
        if checksum.hexdigest() != expected_hash:
            raise ValueError(f"Changed historical exclusion source: {path.name}")
    for split, expected_hash in CMRC_PINS.items():
        raw = (CMRC_DIR / f"cmrc2018_{split}.json").read_bytes()
        if digest(raw) != expected_hash:
            raise ValueError(f"Changed CMRC split: {split}")
        for article in json.loads(raw)["data"]:
            for para in article["paragraphs"]:
                if split != "train" or {article["id"], para["id"]} & cmrc_ids:
                    seen_titles.add(normalize(article["title"]))
                    seen_body_hashes.add(digest(para["context"].encode()))
                if split != "train":
                    continue
                for qa in para["qas"]:
                    upstream.add(normalize(qa["question"]))
                    if article["title"] in study_titles:
                        related_questions[study_titles[article["title"]]["id"]].add(qa["question"])
    prior_ids = {c["doc_id"] for c in prior_cases}
    prior_hashes = {c["document_sha256"] for c in prior_cases}
    prior_questions = {normalize(c["question"]) for c in prior_cases}
    findings = {}
    for c in cases:
        doc = documents[c["doc_id"]]
        body = doc["text"].removeprefix(doc["title"] + "\n\n").removesuffix("\n")
        loc = c["locator"]
        labels = {normalize(t) for t in [doc["title"], *c["topic_aliases_en"]]}
        q = normalize(c["question"])
        checks = {
            "prior_study_document_or_question": c["doc_id"] in prior_ids
            or doc["sha256"] in prior_hashes
            or q in prior_questions,
            "historical_record": loc.get("record") in records or loc.get("article") in cmrc_ids,
            "historical_or_excluded_split_title": bool(labels & seen_titles),
            "historical_or_excluded_split_passage": digest(body.encode()) in seen_body_hashes,
            "historical_question_text": q in questions,
            "upstream_question_text": q in upstream,
        }
        for key, hit in checks.items():
            findings.setdefault(key, [])
            if hit:
                findings[key].append(c["id"])
    related = {k: sorted(v) for k, v in sorted(related_questions.items())}
    return {
        "historical_commit": HISTORICAL_HEAD,
        "historical_json_files": len(files),
        "historical_file_tree_lf_sha256": canonical_digest(files),
        "prior_first_refusal_cases_lf_sha256": lf_digest(PRIOR / "cases.json"),
        "historical_normalized_questions": len(questions),
        "historical_musique_records": len(records),
        "historical_cmrc_passage_ids": len(cmrc_ids),
        "excluded_titles": len(seen_titles),
        "excluded_passage_digests": len(seen_body_hashes),
        "upstream_normalized_questions": len(upstream),
        "same_article_upstream_questions": sum(map(len, related.values())),
        "same_article_upstream_questions_sha256": canonical_digest(related),
        "overlaps": findings,
    }


def verify() -> dict:
    cases, docs = load_cases(), load_documents()
    assert len(cases) == len(docs) == len({d["sha256"] for d in docs.values()}) == 100
    assert (
        len({norm(d["title"]) for d in docs.values()})
        == len({norm(c["question"]) for c in cases})
        == 100
    )
    assert [c["id"] for c in cases] == [f"B{n:03d}" for n in range(1, 101)]
    assert Counter((c["language"], c["answerable"]) for c in cases) == {
        (lang, status): 25 for lang in ("en", "zh") for status in (True, False)
    }
    assert len({c["locator"].get("record", c["locator"].get("article")) for c in cases}) == 100
    topics = {}
    for c in cases:
        lang = c["language"]
        assert c["topic_aliases_en"]
        for label in c["topic_aliases_en"]:
            other = topics.setdefault(norm(label), c["id"])
            assert other == c["id"], ("duplicate topic", label, other, c["id"])
        doc = docs[c["doc_id"]]
        positive = c["answerable"]
        assert doc["sha256"] == c["document_sha256"] and bool(c["facets"]) == positive
        assert len(c["question"]) > 7 and c["question"].endswith("？" if lang == "zh" else "?")
        assert bool(HAN.search(c["question"])) == (lang == "zh") and len(c["target"]) > 2
        assert c["source_kind"] == {"zh": "cmrc2018", "en": "musique"}[lang]
        ref = {"id": 1, "doc_id": doc["id"], "text": doc["text"]}
        assert evidence_covers(c, [ref]) == positive
        assert not evidence_covers(c, []) and not evidence_covers(c, [{**ref, "doc_id": "other"}])
        for f in c["facets"]:
            assert f["aliases"] and f["support"]
            assert all(0 <= a < b <= len(doc["text"]) for a, b in f["support"])
        for response_lang, refusal in (("zh", "证据不足。"), ("en", "Insufficient evidence.")):
            s = score_case(c, state="refusal", answer=refusal, citations=[])
            assert s["valid"] and s["refused"] and not s["needs_review"]
            assert s["wrong_refusal"] == positive and s["answer_correct"] != positive
            assert s["language_match"] == (lang == response_lang)
            assert s["joint_correct"] == (not positive and lang == response_lang)
        for state in ("invalid", "error"):
            s = score_case(c, state=state, answer="Insufficient evidence.", citations=[])
            assert not s["valid"] and not s["joint_correct"]
        if positive:
            answer = c["target"] + " [1]"
            s = score_case(c, state="answer", answer=answer, citations=[ref])
            assert s["joint_correct"] and s["needs_review"], c["id"]
            for bad in (
                {**ref, "doc_id": "other"},
                {**ref, "text": doc["title"]},
                {**ref, "text": "虚构 excerpt"},
            ):
                assert not score_case(c, state="answer", answer=answer, citations=[bad])[
                    "joint_correct"
                ]
            for bad in (
                answer.replace("[1]", "[2]"),
                answer + " 虚构事实。",
                answer + " Invented.",
            ):
                assert not score_case(c, state="answer", answer=bad, citations=[ref])[
                    "joint_correct"
                ]
            for neutral in c["neutral_aliases"]:
                assert language_match(c, neutral + " [1]")
        else:
            assert score_case(c, state="answer", answer="A guess [1].", citations=[ref])[
                "unsafe_answer"
            ]
            assert not score_case(c, state="refusal", answer="Two.", citations=[])["joint_correct"]
            assert not score_case(c, state="refusal", answer="证据不足。 [1]", citations=[ref])[
                "valid"
            ]
    for word, alias, expected in (
        ("是2000名员工", "2000", True),
        ("12000名", "2000", False),
        ("northern", "no", False),
    ):
        assert has_alias(word, [alias]) == expected
    pair = next(c for c in cases if c["family"] == "clause_roles")
    document = docs[pair["doc_id"]]["text"]
    refs = []
    for i, facet in enumerate(pair["facets"], 1):
        a, b = facet["support"][0]
        refs.append({"id": i, "doc_id": pair["doc_id"], "text": document[a:b]})
    claim = "从句主格用 no [1]，主句主格用 wa [2]。"
    assert score_case(pair, state="answer", answer=claim, citations=refs)["joint_correct"]
    swapped = [{**ref, "text": refs[1 - i]["text"]} for i, ref in enumerate(refs)]
    assert not score_case(pair, state="answer", answer=claim, citations=swapped)["joint_correct"]
    return {
        "cases": 100,
        "distinct_documents": 100,
        "language_disposition": {
            "en_answerable": 25,
            "en_unanswerable": 25,
            "zh_answerable": 25,
            "zh_unanswerable": 25,
        },
        "cases_sha256": canonical_digest(cases),
        "documents_sha256": canonical_digest({k: d["sha256"] for k, d in docs.items()}),
        "max_document_bytes": max(len(d["text"].encode()) for d in docs.values()),
        "source_sha256": {k: pin for k, (_, pin) in SOURCES.items()},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    report = verify()
    freeze = json.loads((HERE / "source_freeze.json").read_bytes())
    if report != freeze["dataset"] or artifact_bindings() != freeze["artifacts_lf_sha256"]:
        raise SystemExit("Frozen dataset or scoring artifacts changed")
    if args.history:
        exposure = audit_history()
        if exposure != freeze["exposure"] or any(exposure["overlaps"].values()):
            raise SystemExit("Historical exposure differs from the frozen population audit")
        report["exposure"] = exposure
    print(json.dumps(report, ensure_ascii=False, indent=2))
