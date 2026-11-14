"""Freeze source-isolated QASPER questions before inspecting any candidate outputs."""

import argparse
import hashlib
import itertools
import json
import re
import subprocess
from pathlib import Path

from benchmarks.public_rag.qa_first_response_cases import PUBLIC, ROOT, TRAIN, paper_text, read, select

OUT = PUBLIC / "rag-product-evidence-20261114"
PREVIOUS = "e0fe09bbe0c7ff5b674a204606f038524b63065f"
SOURCE_SHA = "9458bfe76074a8fa8d1685af02bcc73537aa6d338ad20591dfaff1946bc88bf4"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    with path.open("x", encoding="utf8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def archive(name):
    path = "benchmarks/public_rag/results/published/refusal-consistency-20261114/" + name
    return json.loads(subprocess.check_output(["git", "show", f"{PREVIOUS}:{path}"], cwd=ROOT))


def words(question):
    return frozenset(re.findall(r"\w+", question.casefold()))


def close_wording(tokens, existing):
    return any(len(tokens & other) >= 0.80 * len(tokens | other) for other in existing)


def cohort():
    if digest(TRAIN.read_bytes()) != SOURCE_SHA:
        raise ValueError("Unexpected QASPER TRAIN release")
    raw, history, last = read(TRAIN), archive("exposure.json"), archive("protocol.json")
    papers = set(history["excluded_papers"]).union(*last["corpora"].values())
    questions = set(history["excluded_questions"]).union(c["id"] for c in last["cases"])
    past_wordings = {
        words(q["question"])
        for p, doc in raw.items()
        for q in doc["qas"]
        if p in papers or q["question_id"] in questions
    }
    # Filter near copies in a fixed order, even if both official IDs are new.
    observed = set(past_wordings)
    for p, doc in sorted(raw.items()):
        if p in papers:
            continue
        for q in sorted(doc["qas"], key=lambda row: row["question_id"]):
            shape = words(q["question"])
            if close_wording(shape, observed):
                questions.add(q["question_id"])
            else:
                observed.add(shape)
    cases = select(raw, papers, questions, "agenthub-20261114-confirmation-v1|", 24, 16)
    schedule = list(itertools.permutations("BCD"))
    counters = {False: 0, True: 0}
    for case in cases:
        k = counters[case["negative"]]
        case["schedule"] = schedule[k % len(schedule)]
        counters[case["negative"]] += 1
        paper = case["gold_documents"][0]
        case["source_sha256"] = digest(paper_text(raw[paper]).encode())
    return {
        "data_sha256": SOURCE_SHA,
        "prior_exposure_commit": PREVIOUS,
        "excluded_papers": len(papers),
        "excluded_question_ids_after_wording_filter": len(questions),
        "unit": "one question per distinct paper; QASPER TRAIN, never official TEST",
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = cohort()
    output = OUT / "cohort.json"
    if args.verify:
        if read(output) != json.loads(json.dumps(result)):
            raise ValueError("Frozen definitions differ")
    else:
        OUT.mkdir(exist_ok=True)
        save(output, result)
    print(f"{len(result['cases'])} sources; {digest(output.read_bytes())}")


if __name__ == "__main__":
    main()
