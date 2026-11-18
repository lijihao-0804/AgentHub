"""Build arm- and phase-blind packets and apply judgments to the exact observed replies."""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from benchmarks.retrieval.experiments.first_refusal_100.attempts import digest_json, reconcile
from benchmarks.retrieval.experiments.first_refusal_100.runner import file_hash
from packages.core.canonical.json_parse import load_unique_json

from .evidence import observations, text_hash
from .policy import expected_prompt
from .runner import MANIFEST, SCRATCH, material, order, project, smoke_material


def read_json(path):
    return load_unique_json(path.read_text(encoding="utf8"))


def rows(path):
    return [load_unique_json(line.decode("utf8")) for line in path.read_bytes().splitlines()]


def dump(path, value):
    with path.open("x", encoding="utf8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def private(path):
    if path.resolve() == SCRATCH or not path.resolve().is_relative_to(SCRATCH):
        raise ValueError("Review packets and private wire files must remain under .scratch.")


# Match only complete JSON string characters, including complete escape sequences.
_STRING_BODY = re.compile(r'(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*')


def answer_fragment(content):
    """Recover only an observed top-level answer string, including an unfinished string prefix."""
    data = re.sub(r"^```(?:json)?[ \t]*\r?\n", "", content.lstrip(), count=1, flags=re.I).lstrip()
    if not data.startswith("{"):
        return ""
    decoder = json.JSONDecoder()
    remaining = data[1:].lstrip()
    found = None
    while remaining.startswith('"'):
        try:
            key, end = decoder.raw_decode(remaining)
        except ValueError:
            break
        remaining = remaining[end:].lstrip()
        if not remaining.startswith(":"):
            break
        remaining = remaining[1:].lstrip()
        if key == "answer":
            if found is not None or not remaining.startswith('"'):
                return ""
            match = _STRING_BODY.match(remaining, 1)
            # Delimiters decode the complete observed characters. They are never exposed as text.
            found = json.loads('"' + match.group() + '"')
            found = re.split(r"[\ud800-\udfff]", found, maxsplit=1)[0]
            end = match.end()
            if not remaining[end:].startswith('"'):
                break
            end += 1
        else:
            # Skip entire values, so nested answer fields in tool or reasoning data cannot match.
            try:
                _, end = decoder.raw_decode(remaining)
            except ValueError:
                break
        remaining = remaining[end:].lstrip()
        if not remaining.startswith(","):
            break
        remaining = remaining[1:].lstrip()
    return found or ""


def blinded_unit(outcome, provider_content=None):
    unit = {key: outcome[key] for key in ("state", "answer", "citations")}
    if unit["answer"].strip():
        return unit
    if unit["state"] in {"answer", "refusal"}:
        raise ValueError("A valid answer must carry a visible reply.")
    if unit["state"] == "invalid" and provider_content:
        fragment = answer_fragment(provider_content)
        if fragment.strip():
            return {**unit, "answer": fragment, "answer_field_fragment": True}
    return None


def packets(run, manifest):
    private(run)
    execution = read_json(run / "execution.json")
    checks = execution["private_file_sha256"]
    if set(checks) != {
        "freeze.json",
        "wire-private.jsonl",
        "http-private.jsonl",
        "derived.jsonl",
        "receipts.jsonl",
    } or any(file_hash(run / name) != sha for name, sha in checks.items()):
        raise ValueError("Private records differ from the completed execution.")
    if any(execution[k] != value for k, value in reconcile(run).items()):
        raise ValueError("The independent audit differs from the execution summary.")
    freeze = read_json(run / "freeze.json")
    offline = freeze["offline"]
    cases, contents, scorer, covers = smoke_material() if offline else material(manifest)
    if (
        execution["offline"] != offline
        or execution["unknowns"] != 0
        or freeze["cases_sha256"] != digest_json(cases)
        or set(freeze["inputs"]) != set(contents)
        or any(
            freeze["inputs"][k]["source_sha256"] != text_hash(v.decode("utf8"))
            for k, v in contents.items()
        )
    ):
        raise ValueError("The review corpus differs from the frozen, completed experiment.")
    completed = [r for r in rows(run / "http-private.jsonl") if r["state"] == "returned"]
    grouped_wire = defaultdict(list)
    for event in rows(run / "wire-private.jsonl"):
        grouped_wire[event["key"]].append(event)
    automatic = rows(run / "derived.jsonl")
    plan = [(c, arm) for c, arms in order(cases) for arm in arms]
    keys = {f"{c['id']}/{arm}" for c, arm in plan}
    if len(keys) != 2 * len(cases) or set(grouped_wire) != keys:
        raise ValueError("The blinded inventory does not cover both arms exactly once.")
    by_case = {}
    mapping = {}
    for (case, arm), returned, recorded in zip(plan, completed, automatic, strict=True):
        qid, source_id = case["id"], case["doc_id"]
        key = f"{qid}/{arm}"
        system = expected_prompt(arm, case["question"])
        if returned["key"] != key or text_hash(system) != freeze["prompt_sha256"][qid][arm]:
            raise ValueError("Question order or policy differs from its executed freeze.")
        events, doc = grouped_wire[key], freeze["inputs"][source_id]
        results = observations(case, doc, events, returned, system)
        if recorded != project(
            case, arm, events, returned, results, scorer, covers, offline=offline
        ):
            raise ValueError("The published projection differs from its actual answer or evidence.")
        first_attempts, final = results
        visible = returned["evidence"]
        packet = by_case.setdefault(
            qid,
            {
                "case": case,
                "source": contents[source_id].decode("utf8"),
                "visible_evidence": visible,
                "outputs": {},
            },
        )
        if packet["visible_evidence"] != visible:
            raise ValueError("Arms of one question were shown different evidence.")
        for stage, outcome in (("first", first_attempts[0]), ("final", final)):
            unit = blinded_unit(outcome, events[0]["content"] if stage == "first" else None)
            if unit is None:
                continue
            # Equivalent answers share a judgment only with identical state, numbered evidence
            # and citation excerpts. Neither arm nor first/final status enters the unit ID.
            uid = digest_json([case, visible, unit])[:24]
            if packet["outputs"].setdefault(uid, {"uid": uid, **unit}) != {"uid": uid, **unit}:
                raise ValueError("Blinded unit identifier collision.")
            mapping[f"{qid}/{arm}/{stage}"] = uid
    result = []
    for qid in sorted(by_case, key=digest_json):
        packet = by_case[qid]
        packet["outputs"] = [packet["outputs"][uid] for uid in sorted(packet["outputs"])]
        result.append(packet)
    return [result[::2], result[1::2]], mapping, automatic


def prepare(run, review, manifest=MANIFEST):
    private(review)
    parts, mapping, _ = packets(run, manifest)
    review.mkdir(parents=True, exist_ok=False)
    for number, part in enumerate(parts, 1):
        dump(review / f"packet-{number}.json", part)
    dump(review / "mapping-private.json", mapping)
    return {"questions": sum(map(len, parts)), "distinct_visible_units": len(set(mapping.values()))}


def validate_verdict(value, *, final):
    keys = {"value", "support", "language_match", "reason"}
    if final and isinstance(value, dict) and value.get("unresolved") is True:
        flags = [value.get(k) for k in ("value", "support", "language_match")]
        if (
            set(value) == keys | {"unresolved"}
            and None in flags
            and all(v is None or type(v) is bool for v in flags)
            and isinstance(value["reason"], str)
            and value["reason"].strip()
        ):
            return
    if (
        not isinstance(value, dict)
        or not keys.issubset(value)
        or set(value) - keys - {"uncertain"}
        or any(type(value[k]) is not bool for k in ("value", "support", "language_match"))
        or not isinstance(value["reason"], str)
        or (
            not all(value[k] for k in ("value", "support", "language_match"))
            and not value["reason"].strip()
        )
        or type(value.get("uncertain", False)) is not bool
        or (final and value.get("uncertain", False))
    ):
        raise ValueError(
            "Expected three binary judgments and a reason for a failed or disputed unit."
        )


def apply(run, review, output, manifest=MANIFEST):
    private(review)
    parts, mapping, automatic = packets(run, manifest)
    if read_json(review / "mapping-private.json") != mapping:
        raise ValueError("The unblinding key differs from the original evidence and output.")
    verdicts = {}
    fragment_hashes = {}
    extra_path = review / "additional-verdicts.json"
    extra = read_json(extra_path) if extra_path.exists() else {}
    for number, part in enumerate(parts, 1):
        if read_json(review / f"packet-{number}.json") != part:
            raise ValueError("A packet's blinded response or evidence was modified.")
        expected = {unit["uid"] for p in part for unit in p["outputs"]}
        fragment_hashes.update(
            {
                unit["uid"]: text_hash(unit["answer"])
                for p in part
                for unit in p["outputs"]
                if unit.get("answer_field_fragment")
            }
        )
        entries = read_json(review / f"verdicts-{number}.json")
        if entries.keys() & extra.keys():
            raise ValueError("A supplementary judgment cannot replace an existing decision.")
        for uid in (expected - entries.keys()) & extra.keys():
            entries[uid] = extra.pop(uid)
        if set(entries) != expected or verdicts.keys() & entries.keys():
            raise ValueError("Each blinded response must receive exactly its assigned judgment.")
        for decision in entries.values():
            validate_verdict(decision, final=False)
        verdicts.update(entries)
    if extra:
        raise ValueError("A supplementary judgment refers to an unknown blinded unit.")
    pending = {uid for uid, v in verdicts.items() if v.get("uncertain")}
    adjudicated = (
        read_json(review / "adjudications.json") if (review / "adjudications.json").exists() else {}
    )
    if pending != set(adjudicated):
        raise ValueError(
            "Every question flagged as uncertain needs an independent blinded adjudication."
        )
    for value in adjudicated.values():
        validate_verdict(value, final=True)
    verdicts.update(adjudicated)
    for row in automatic:
        for stage in ("first", "final"):
            item = row[stage]
            uid = mapping.get(f"{row['id']}/{row['arm']}/{stage}")
            if uid is None:
                continue
            verdict = verdicts[uid]
            scores = item["scores"]
            valid = scores["valid"] and item["state"] in ("answer", "refusal")
            correct_state = row["answerable"] == (item["state"] == "answer")
            value = valid and correct_state and verdict["value"] is True
            support = valid and verdict["support"] is True
            language = verdict["language_match"]
            unresolved = verdict.get("unresolved", False)
            automatic_grade = (
                scores["answer_correct"],
                scores["citation_correct"],
                scores["language_match"],
            )
            if automatic_grade != (value, support, language) and not verdict["reason"].strip():
                raise ValueError(
                    "A blinded judgment overriding the automatic grade requires a reason."
                )
            scores.update(
                answer_correct=value,
                citation_correct=support,
                semantic_and_citation_correct=value and support,
                language_match=language,
                joint_correct=value and support and language is True,
                value_with_language=value and language is True,
                citation_with_language=support and language is True,
                needs_review=unresolved,
            )
            # Review notes may quote private sources or replies; retain them in the private files.
            item["semantic_review"] = {
                "uid": uid,
                "value": verdict["value"],
                "support": verdict["support"],
                "language_match": language,
                "adjudicated": uid in adjudicated,
                "unresolved": unresolved,
                "reason_sha256": text_hash(verdict["reason"]),
            }
            if uid in fragment_hashes:
                item["semantic_review"]["visible_answer_fragment_sha256"] = fragment_hashes[uid]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf8", newline="\n") as dest:
        for row in automatic:
            dest.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {
        "reviewed_units": len(verdicts),
        "adjudicated_units": len(adjudicated),
        "unresolved_units": sum(v.get("unresolved", False) for v in adjudicated.values()),
        "paired_rows": len(automatic),
    }


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("operation", choices=("prepare", "apply"))
    cli.add_argument("--run", type=Path, required=True)
    cli.add_argument("--review", type=Path, required=True)
    cli.add_argument("--manifest", type=Path, default=MANIFEST)
    cli.add_argument("--output", type=Path)
    args = cli.parse_args()
    if args.operation == "prepare":
        info = prepare(args.run, args.review, args.manifest)
    else:
        if args.output is None:
            cli.error("apply requires --output for the sanitized per-arm records.")
        info = apply(args.run, args.review, args.output, args.manifest)
    print(json.dumps(info))
