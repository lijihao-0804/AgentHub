"""Prepare arm-blind semantic reviews and bind judgments back to recorded visible replies."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import httpx

from .attempts import digest_json, read_attempt, read_http
from .dataset import load_cases, load_documents


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def prepare(run, dest):
    dest.mkdir(parents=True, exist_ok=False)
    cases = {c["id"]: c for c in load_cases()}
    docs = load_documents()
    inputs = json.loads((run / "freeze.json").read_bytes())["inputs"]
    first = {e["key"]: e for e in rows(run / "wire-private.jsonl") if e["attempt"] == 1}
    items = defaultdict(dict)
    mapping = {}
    for item in rows(run / "http-private.jsonl"):
        if item["state"] != "returned":
            continue
        key = item["key"]
        qid = key.split("/")[0]
        case = cases[qid]
        doc = case["doc_id"]
        observations = {
            "first": read_attempt(first[key], item["evidence"]),
            "final": read_http(httpx.Response(item["http"], json=item["body"]), inputs[doc], doc),
        }
        for phase, obs in observations.items():
            if obs["state"] not in ("refusal", "answer"):
                continue
            unit = {
                "state": obs["state"],
                "answer": obs["answer"],
                "citations": [
                    {"id": c["id"], "doc_id": c["doc_id"], "text": c["text"]}
                    for c in obs["citations"]
                ],
            }
            uid = digest_json([qid, unit])[:20]
            items[qid][uid] = {"uid": uid, **unit}
            mapping[key + "/" + phase] = uid
    if len(first) != 200 or len(cases) != 100:
        raise ValueError("The campaign must be complete before arm-blind review.")
    packets = []
    for qid in sorted(cases, key=digest_json):
        case = cases[qid]
        packets.append(
            {
                "case": case,
                "source": docs[case["doc_id"]]["text"],
                "outputs": [items[qid][k] for k in sorted(items[qid])],
            }
        )
    for idx in range(2):
        dump(dest / f"packet-{idx + 1}.json", packets[idx::2])
    dump(dest / "mapping-private.json", mapping)


def apply(run, review, output):
    mapping = json.loads((review / "mapping-private.json").read_bytes())
    verdicts = {}
    for i in (1, 2):
        entries = json.loads((review / f"verdicts-{i}.json").read_bytes())
        for uid, decision in entries.items():
            if uid in verdicts or any(
                type(decision.get(k)) is not bool for k in ("value", "support")
            ):
                raise ValueError("Duplicate or incomplete semantic judgment.")
            verdicts[uid] = decision
    if set(verdicts) != set(mapping.values()):
        raise ValueError("Review coverage does not match the blinded visible responses.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf8") as dest:
        for item in rows(run / "derived.jsonl"):
            for stage in ("first", "final"):
                response = item[stage]
                if response["state"] in ("answer", "refusal"):
                    uid = mapping[f"{item['id']}/{item['arm']}/{stage}"]
                    decision = verdicts[uid]
                    value = decision["value"] and (
                        item["answerable"] == (response["state"] == "answer")
                    )
                    response["semantic_review"] = {"uid": uid, **decision}
                    response["scores"].update(
                        answer_correct=value,
                        citation_correct=decision["support"],
                        joint_correct=value and decision["support"],
                        needs_review=False,
                    )
            dest.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("operation", choices=("prepare", "apply"))
    cli.add_argument("--run", type=Path, required=True)
    cli.add_argument("--review", type=Path, required=True)
    cli.add_argument("--output", type=Path)
    args = cli.parse_args()
    if args.operation == "prepare":
        if not args.review.resolve().is_relative_to(Path(".scratch").resolve()):
            cli.error("Private source excerpts and review packets belong under .scratch.")
        prepare(args.run, args.review)
    elif args.output is None:
        cli.error("apply requires --output")
    else:
        apply(args.run, args.review, args.output)
