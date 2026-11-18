"""Blind first/final review, with independent provenance checks on delivered citations."""

import argparse
import json
import re
from pathlib import Path

from benchmarks.retrieval.experiments.output_limit.cases import checked, digest
from benchmarks.retrieval.experiments.output_limit.grading import score
from packages.core.canonical.json_parse import load_unique_json

ROOT = Path(__file__).resolve().parents[4]
HISTORY = ROOT / ".scratch/output-limit-20261119"
DIRECTORY = ROOT / ".scratch/first-refusal-20261119"
PRIOR_JUDGMENTS = "d79ac0c76453c3c99f6a4ca51a06120834aaf27b0efce823c3525e7f85f38165"
ARMS = ("baseline", "candidate")
STAGES = ("first", "final")


def load(path):
    return json.loads(path.read_bytes())


def lines(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf8")


def cited_evidence(case, citations, *, final):
    supplied = {s["id"]: s for s in case["selected"]}
    cited = []
    for item in citations:
        source = supplied[item["id"]]
        if final:
            for name, key in (
                ("document_id", "document"),
                ("document_revision_id", "revision"),
                ("chunk_id", "chunk_id"),
                ("source", "source"),
            ):
                if item[name] != source[key]:
                    raise ValueError("Endpoint returned a different source identity")
            text = item["excerpt"]
            if not text or not source["text"].startswith(text):
                raise ValueError("Endpoint excerpt differs from the evidence sent to the model")
        else:
            text = item["text"]
            if text != source["text"] or item["source"] != source["source_id"]:
                raise ValueError("First-stage citation differs from the sent evidence")
        if item["locator"] != source["locator"]:
            raise ValueError("Citation locator drift")
        cited.append(
            dict(id=item["id"], source=source["source_id"], text=text, locator=item["locator"])
        )
    if len(cited) != len({item["id"] for item in cited}):
        raise ValueError("Duplicate delivered citation")
    return cited


def material(case, row, stage):
    if stage == "first":
        observed = row["first"]
        if not observed["valid"]:
            return None, observed["reason"]
        answer, refused = observed["answer"], observed["refused"]
        cited = cited_evidence(case, observed["cited"], final=False)
    else:
        if row["http"] != 200:
            return None, f"HTTP_{row['http']}"
        body = row["body"]
        answer, refused = body["answer"], body["insufficient_evidence"]
        cited = cited_evidence(case, body["citations"], final=True)
    if type(refused) is not bool or not answer.strip() or bool(cited) == refused:
        raise ValueError("Delivered answer violates the citation/refusal contract")
    return dict(
        question=case["question"],
        language=case["language"],
        gold=case["gold"],
        valid=True,
        refused=refused,
        answer=answer,
        cited=cited,
    ), None


def packets(cases, outcomes):
    index = {case["id"]: case for case in cases}
    if len(index) != len(cases):
        raise ValueError("Duplicate registered identity")
    seen, blind, keys = {}, {}, []
    for row in outcomes:
        case = index[row["id"]]
        key = (case["id"], row["arm"])
        if key in seen or row["arm"] not in ARMS or row["cap"] != 2048:
            raise ValueError("Duplicate or unregistered treatment")
        if not 1 <= len(row["events"]) <= 2:
            raise ValueError("Incomplete or unexpected model-call path")
        first_wire = row["events"][0]
        seen[key] = (first_wire["id"], first_wire["request_sha256"], first_wire["content"])
        if first_wire["messages_sha256"] != case["message_sha256"]:
            raise ValueError("First call does not match frozen messages")
        try:
            model = load_unique_json(first_wire["content"])
            raw_flag = model.get("insufficient_evidence") if isinstance(model, dict) else None
            raw_cited_refusal = raw_flag is True and bool(
                model.get("citation_ids") or re.search(r"\[-?\d+\]", str(model.get("answer", "")))
            )
        except (TypeError, ValueError, RecursionError):
            raw_flag = None
            raw_cited_refusal = False
        for stage in STAGES:
            packet, reason = material(case, row, stage)
            uid = digest(packet)
            if packet is not None:
                blind[uid] = dict(uid=uid, **packet)
            keys.append(
                dict(
                    id=row["id"],
                    qid=row["qid"],
                    arm=row["arm"],
                    stage=stage,
                    uid=uid,
                    valid=packet is not None,
                    refused=bool(packet is not None and packet["refused"]),
                    answerable=case["gold"]["answerable"],
                    mode=row["mode"],
                    language_expected=case["language"],
                    reason=reason,
                    physical_needed=len(row["events"]),
                    http=row["http"],
                    raw_model_declared_refusal=raw_flag,
                    raw_cited_refusal=raw_cited_refusal,
                    rule=row["first"].get("rule"),
                )
            )
    if set(seen) != {(case_id, arm) for case_id in index for arm in ARMS}:
        raise ValueError("Missing registered pair")
    if any(seen[case_id, ARMS[0]] != seen[case_id, ARMS[1]] for case_id in index):
        raise ValueError("Paired arms did not receive the same physical first response")
    return blind, keys


def known_and_new(blind):
    prior = json.loads(checked(HISTORY / "judgments.json", PRIOR_JUDGMENTS))
    known = [row for row in prior if row["uid"] in blind]
    ids = {r["uid"] for r in known}
    return known, [v for k, v in sorted(blind.items()) if k not in ids]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("prepare", "score"))
    ap.add_argument("--directory", type=Path, default=DIRECTORY)
    args = ap.parse_args()
    d = args.directory
    blind, keys = packets(load(d / "cases.json"), lines(d / "outcomes.jsonl"))
    known, pending = known_and_new(blind)
    review = d / "review"
    if args.action == "prepare":
        review.mkdir(exist_ok=True)
        for i in range(3):
            save(review / f"panel-{i}.json", pending[i::3])
        print(
            dict(stages=len(keys), unique_valid=len(blind), inherited=len(known), new=len(pending))
        )
        return
    reviewed = [r for path in sorted(review.glob("labels-*.json")) for r in load(path)]
    revised = {r["uid"]: r for path in sorted(review.glob("adjudicated*.json")) for r in load(path)}
    if set(revised) - {r["uid"] for r in reviewed}:
        raise ValueError("Adjudication refers to an unknown review packet")
    labels = known + [revised.get(r["uid"], r) for r in reviewed]
    grades = score(keys, labels)
    save(d / "judgments.json", labels)
    save(d / "grades.json", grades)
    print(dict(stages=len(grades), reviewed=len(reviewed), inherited=len(known)))


if __name__ == "__main__":
    main()
