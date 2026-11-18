"""Prepare arm-blind first and final QA observations using the actual cited text."""

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import httpx

from benchmarks.retrieval.table_header_local_qa import score as score_cell
from packages.knowledge.citation_qa import _InvalidCitationAnswer, resolve_citation_answer

ROOT = Path(__file__).resolve().parents[4]
PRIVATE = ROOT / ".scratch"
GROUPS = ("fresh", "R", "B", "W")


def load(path):
    return json.loads(path.read_bytes())


def save(path, data):
    with path.open("x", encoding="utf8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def cases(group):
    if group == "fresh":
        return [
            r
            for r in load(PRIVATE / "joint-six-20261118/matched.json")["cases"]
            if r["cohort"] != "smoke"
        ]
    if group == "W":
        return [
            dict(id=f"W{i:02}", question=r[0], value=r[3], cell=r[4], answerable=r[3] is not None)
            for i, r in enumerate(
                load(ROOT / "benchmarks/retrieval/table_header_protocol.json")["cases"], 1
            )
        ]
    folder = "first-refusal-100-docs" if group == "R" else "bilingual-refusal-100-docs"
    return load(PRIVATE / folder / "manifest.json")["cases"]


def observed_stages(case, row):
    selected = {r["id"]: r for r in row["selected"]}
    if len(selected) != len(row["selected"]) or len(selected) > 6:
        raise ValueError("Duplicate IDs or too many passages")
    invalid = dict(valid=False, answer="", refused=False, citations=[])
    event = row["events"][0]
    first = dict(invalid, failure="empty_output")
    if isinstance(event["content"], str) and event["content"].strip():
        try:
            r = resolve_citation_answer(
                case["question"],
                event["content"],
                visible_ids=set(selected),
                output_limit_reached=event["finish"] == "length",
            )
            first = dict(
                valid=True,
                answer=r.answer,
                refused=r.insufficient_evidence,
                citations=[selected[i] for i in r.citation_ids],
            )
        except _InvalidCitationAnswer as exc:
            first = dict(invalid, failure=exc.reason.value)
    final = dict(invalid, failure=row["body"].get("error", {}).get("code", f"http_{row['http']}"))
    if row["http"] == 200:
        body = row["body"]
        cited = []
        for entry in body["citations"]:
            original = selected[entry["id"]]
            if (entry["chunk_id"], entry["document_revision_id"], entry["locator"]) != (
                original["chunk_id"],
                original["revision"],
                original["locator"],
            ) or not original["text"].startswith(entry["excerpt"]):
                raise ValueError("Published citation does not match its visible chunk")
            cited.append(dict(original, text=entry["excerpt"]))
        final = dict(
            valid=True,
            answer=body["answer"],
            refused=body["insufficient_evidence"],
            citations=cited,
        )
    return {"first": first, "final": final}


def without_citations(text):
    return re.sub(r"\[\d+\]", "", text)


def matches(text, aliases):
    def tokens(value):
        return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))

    normalized = " " + tokens(without_citations(text)) + " "
    return any(" " + t + " " in normalized for alias in aliases if (t := tokens(alias)))


def automatic(group, case, result):
    cited = result["citations"]
    if not result["valid"]:
        return {"value": False, "support": False}
    if result["refused"]:
        return {"value": not case["answerable"], "support": False}
    if group == "W":
        answer = httpx.Response(
            200,
            json={
                "answer": result["answer"],
                "insufficient_evidence": False,
                "citations": [dict(locator=x["locator"], excerpt=x["text"]) for x in cited],
            },
        )
        scores = score_cell(answer, case["value"], case["cell"])
        return dict(
            value=scores["value_correct"], support=scores["cited_cell"] and case["answerable"]
        )
    if not case["answerable"]:
        return dict(value=False, support=False)
    if group == "fresh":

        def covered(gold):
            spans = sorted(
                (x["locator"]["char_start"], x["locator"]["char_start"] + len(x["text"]))
                for x in cited
                if x["source_id"] == gold["source_id"]
                and isinstance(x["locator"].get("char_start"), int)
            )
            end = gold["start"]
            for left, right in spans:
                if left > end:
                    break
                end = max(end, right)
            return end >= gold["end"]

        return dict(
            value=matches(result["answer"], [case["answer"], *case["answer_aliases"]]),
            support=all(covered(g) for g in case["gold_spans"]),
        )
    name = "first-refusal-100-docs" if group == "R" else "bilingual-refusal-100-docs"
    text = (PRIVATE / name / (case["doc_id"] + ".txt")).read_text(encoding="utf8")
    same_doc = [" ".join(x["text"].split()) for x in cited if x["source_id"] == case["doc_id"]]
    return dict(
        value=all(matches(result["answer"], f["aliases"]) for f in case["facets"]),
        support=all(
            any(
                " ".join(text[left:right].split()) in excerpt
                for left, right in f["support"]
                for excerpt in same_doc
            )
            for f in case["facets"]
        ),
    )


def prepare(directory, output, partial=False):
    observations, packets, expected = [], {}, set()
    for group in GROUPS:
        by_id = {c["id"]: c for c in cases(group)}
        arms = ("baseline", "candidate") if group == "fresh" else ("candidate",)
        expected.update(
            (qid, arm, stage) for qid in by_id for arm in arms for stage in ("first", "final")
        )
        path = directory / (group + ".jsonl")
        if partial and not path.is_file():
            continue
        for row in rows(path):
            case = by_id[row["id"]]
            gold = {
                k: case[k]
                for k in (
                    "answerable",
                    "language",
                    "target",
                    "facets",
                    "value",
                    "cell",
                    "question_decomposition",
                    "answer",
                    "answer_aliases",
                )
                if k in case
            }
            for stage, r in observed_stages(case, row).items():
                cited = [
                    dict(id=c["id"], text=c["text"], source=c["source_id"], locator=c["locator"])
                    for c in r["citations"]
                ]
                packet = dict(
                    question=case["question"],
                    gold=gold,
                    answer=r["answer"],
                    valid=r["valid"],
                    refused=r["refused"],
                    cited=cited,
                )
                uid = hashlib.sha256(
                    json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                packets[uid] = dict(uid=uid, **packet)
                observations.append(
                    dict(
                        id=case["id"],
                        arm=row["arm"],
                        group=group,
                        stage=stage,
                        uid=uid,
                        screen=automatic(group, case, r),
                        failure=r.get("failure"),
                    )
                )
    observed = {(r["id"], r["arm"], r["stage"]) for r in observations}
    if (
        len(observations) != len(observed)
        or not observed <= expected
        or (not partial and observed != expected)
    ):
        raise ValueError("Formal responses do not match the registered question arms")
    output.mkdir(exist_ok=False, parents=True)
    save(output / "keys.json", observations)
    save(
        output / "coverage.json",
        dict(
            expected_stages=len(expected),
            observed_stages=len(observed),
            missing=sorted(expected - observed),
        ),
    )
    unique = [packets[k] for k in sorted(packets)]
    for i in range(3):
        save(output / f"blind-{i}.json", unique[i::3])
    print(f"{len(observations)} scored stages; {len(unique)} unique blinded packets")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--partial", action="store_true", help="Describe only completed rows of a halted campaign."
    )
    args = parser.parse_args()
    prepare(args.directory, args.output, args.partial)
