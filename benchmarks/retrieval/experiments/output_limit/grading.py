"""Grade the two output limits using the same hidden-arm semantic rubric.

Review each unique valid packet with the question, frozen gold and cited text.
Mark value true only for a complete, uncontradicted answer or an explicit,
unqualified abstention on an unanswerable question. Mark language true only
when the answer uses the question's language. Mark support true for a factual
answer only when cited spans establish every material claim and all necessary
links in the reasoning. Verify table headers, entities, units and dates.
Equivalent independently sufficient cited text may support an answer. A
refusal requires no citations. Uncertainty requires adjudication before scoring.
Assess first and final separately, retaining invalid and abstaining responses.
"""

import argparse
import json
from pathlib import Path

from benchmarks.retrieval.experiments.output_limit.cases import REVIEW_HASHES, checked, digest
from packages.knowledge.citation_qa import _InvalidCitationAnswer, resolve_citation_answer


def project(case, content, finish):
    selected = {s["id"]: s for s in case["selected"]}
    if not content or not content.strip():
        return False, False, "", [], "empty_output"
    try:
        answer = resolve_citation_answer(
            case["question"],
            content,
            visible_ids=set(selected),
            output_limit_reached=finish == "length",
        )
    except _InvalidCitationAnswer as exc:
        return False, False, "", [], exc.reason.value
    cited = [
        {
            "text": selected[i]["text"],
            "source": selected[i]["source_id"],
            "locator": selected[i]["locator"],
            "id": i,
        }
        for i in answer.citation_ids
    ]
    return True, answer.insufficient_evidence, answer.answer, cited, None


def make_packets(cases, results):
    """Results are {id,cap,http,body,events}; cases are the exact dispatched rows."""
    index = {c["id"]: c for c in cases}
    unique, keys = {}, []
    observed = set()
    for r in results:
        case = index[r["id"]]
        pair = (r["id"], r["cap"])
        if pair in observed or not r["events"]:
            raise ValueError(f"Duplicate exchange or missing receipt {pair}")
        observed.add(pair)
        for stage, event in (("first", r["events"][0]), ("final", r["events"][-1])):
            valid, refused, answer, cited, reason = project(case, event["content"], event["finish"])
            if stage == "final":
                if r["http"] == 200:
                    body = r["body"]
                    if (valid, answer, refused, [c["id"] for c in cited]) != (
                        True,
                        body["answer"],
                        body["insufficient_evidence"],
                        [c["id"] for c in body["citations"]],
                    ):
                        raise ValueError(f"Final wire and endpoint judgments differ for {pair}")
                    visible = {s["id"]: s for s in case["selected"]}
                    cited = []
                    for citation in body["citations"]:
                        item = visible[citation["id"]]
                        if (
                            any(
                                citation[k] != item[j]
                                for k, j in (
                                    ("document_id", "document"),
                                    ("document_revision_id", "revision"),
                                    ("chunk_id", "chunk_id"),
                                    ("source", "source"),
                                    ("locator", "locator"),
                                )
                            )
                            or not citation["excerpt"]
                            or citation["excerpt"] not in item["text"]
                        ):
                            raise ValueError(f"Unbound endpoint citation for {pair}")
                        cited.append(
                            dict(
                                id=citation["id"],
                                text=citation["excerpt"],
                                locator=citation["locator"],
                                source=item["source_id"],
                            )
                        )
                else:
                    valid, refused, answer, cited, reason = (
                        False,
                        False,
                        "",
                        [],
                        f"HTTP_{r['http']}",
                    )
            packet = dict(
                question=case["question"],
                language=case["language"],
                gold=case["gold"],
                valid=valid,
                refused=refused,
                answer=answer,
                cited=cited,
            )
            uid = digest(packet)
            if valid:
                unique[uid] = dict(uid=uid, **packet)
            keys.append(
                dict(
                    id=r["id"],
                    qid=case.get("qid", r["id"]),
                    cap=r["cap"],
                    stage=stage,
                    uid=uid,
                    valid=valid,
                    refused=refused,
                    reason=reason,
                    cohort=case["cohort"],
                    answerable=case["gold"]["answerable"],
                )
            )
    return sorted(unique.values(), key=lambda row: row["uid"]), keys


def score(keys, judgments):
    labels = {r["uid"]: r for r in judgments}
    if len(labels) != len(judgments) or set(labels) != {r["uid"] for r in keys if r["valid"]}:
        raise ValueError("Missing, duplicate or surplus blind judgments")
    output = []
    for row in keys:
        label = labels.get(row["uid"], {"value": False, "support": False, "language": False})
        if (
            any(type(label[k]) is not bool for k in ("value", "support", "language"))
            or label.get("uncertain")
            or row["valid"]
            and not str(label.get("reason") or "").strip()
        ):
            raise ValueError("A reason and resolved boolean judgments are required")
        match = row["refused"] != row["answerable"]
        value = row["valid"] and match and label["value"]
        strict = value and label["language"] and (row["refused"] or label["support"])
        output.append(
            {
                **row,
                "value": value,
                "support": label["support"],
                "language": label["language"],
                "strict": strict,
            }
        )
    return output


def inherited_labels(repo, blind):
    """Reuse old human judgments only for the same gold, answer, sources and spans."""
    root = Path(repo) / ".scratch/joint-six-review-packets-v1"
    old = {name: json.loads(checked(root / name, sha)) for name, sha in REVIEW_HASHES.items()}
    by_uid = {r["uid"]: r for name, rows in old.items() if name.startswith("blind-") for r in rows}
    lookup = {}
    fields = ("question", "gold", "valid", "refused", "answer", "cited")
    for r in old["aligned-v1.json"]["rows"]:
        if not r["valid"]:
            continue
        packet = by_uid[r["uid"]]
        key = (r["stage"], digest({k: packet[k] for k in fields}))
        verdict = {k: r[k] for k in ("value", "support", "language")}
        if lookup.setdefault(key, verdict) != verdict:
            raise ValueError("Contradictory archived adjudications")
    known, remaining = [], []
    for packet in blind:
        key = digest({k: packet[k] for k in fields})
        # One blind judgment serves every new stage with identical visible
        # materials. Reuse it only when both old stages supply that judgment.
        first, final = lookup.get(("first", key)), lookup.get(("final", key))
        if first is not None and first == final:
            known.append(
                dict(
                    uid=packet["uid"],
                    **first,
                    reason="Identical question, gold, answer and located citations in old review.",
                )
            )
        else:
            remaining.append(packet)
    return known, remaining


def load(path):
    raw = path.read_bytes()
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in raw.splitlines()]
    return json.loads(raw)


def save(path, rows):
    with path.open("x", encoding="utf8") as stream:
        json.dump(rows, stream, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    packets = commands.add_parser("packets")
    for name in ("cases", "results", "blind", "keys"):
        packets.add_argument("--" + name, type=Path, required=True)
    grades = commands.add_parser("score")
    for name in ("keys", "judgments", "output"):
        grades.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "packets":
        blind, keys = make_packets(load(args.cases), load(args.results))
        save(args.blind, blind)
        save(args.keys, keys)
    else:
        save(args.output, score(load(args.keys), load(args.judgments)))
