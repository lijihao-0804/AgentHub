"""Freeze exact prompt inputs locally. The authored documents are research fixtures."""

import argparse
import json
import re
import subprocess
import unicodedata
from collections import Counter
from hashlib import sha256
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
TARGETS = {"W13", "R092", "B006"}
REVIEW_HASHES = {
    "aligned-v1.json": "8208fb1616fc6f640de581db5b58de7299353a2560df8a766815072e5fed5f30",
    "blind-0.json": "8be35bfa4a409e8b4eace31b340a9f3fcd492f053fa87b09214aafd0e0e3637a",
    "blind-1.json": "0cccfd516b6c1bf6e5d554f6c3a8eb254a6d97f74b1dd2025e76d94f91da4f44",
    "blind-2.json": "fe46acf1071d5eece9fd4eb0fe5fb0878f45f9f1c2b24b744f183a71b009fb79",
}


def digest(obj):
    return sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def checked(path, expected):
    data = path.read_bytes()
    if sha256(data).hexdigest() != expected:
        raise ValueError(f"Archive integrity failure: {path}")
    return data


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def fresh_sources():
    rows = [json.loads(line) for line in (FIXTURES / "fresh.jsonl").read_bytes().splitlines()]
    counts = Counter((row["language"], row["gold"]["answerable"]) for row in rows)
    if counts != {(lang, a): 10 for lang in ("zh", "en") for a in (True, False)}:
        raise ValueError("Independent fixture strata changed")
    for values in (
        [r["id"] for r in rows],
        [normalized(r["question"]) for r in rows],
        [normalized(d["text"]) for r in rows for d in r["documents"]],
    ):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate independent fixture")
    return rows


def history_case_index(repo):
    from benchmarks.retrieval.citation_contract_replay import CAPTURE_SHA256, PREFLIGHT_SHA256

    scratch = Path(repo) / ".scratch"
    archive = scratch / "joint-six-qa-20261118"
    review = scratch / "joint-six-review-packets-v1"
    freeze = json.loads(
        checked(
            archive / "formal-freeze.json",
            "9455ee2e3447358343adce8cc33bf9950fcfe5d2f68e79c40b9b07028de33d4b",
        )
    )
    files = {name: json.loads(checked(review / name, h)) for name, h in REVIEW_HASHES.items()}
    packets = {
        r["uid"]: r for name, rows in files.items() if name.startswith("blind-") for r in rows
    }
    scores = {}
    gold = {}
    for row in files["aligned-v1.json"]["rows"]:
        key = (row["group"], row["id"], row["arm"])
        scores.setdefault(key, {})[row["stage"]] = row["strict"]
        packet = packets[row["uid"]]
        current = (packet["question"], packet["gold"])
        if gold.setdefault(key[:2], current) != current:
            raise ValueError(f"Conflicting archive gold {key}")

    controls = {
        r["id"] for r in freeze["history_controls"] if r["current_default_equal_archived_default"]
    }
    if len(controls) != 214:
        raise ValueError("Historical baseline identity incomplete")
    out = {}
    for group in CAPTURE_SHA256:
        bindings = json.loads(
            checked(
                archive / "inputs" / group / "bindings.json", freeze["artifacts"][group]["bindings"]
            )
        )["bindings"]
        for directory, expected in (
            ("paid-formal", CAPTURE_SHA256),
            ("preflight", PREFLIGHT_SHA256),
        ):
            lines = checked(archive / directory / f"{group}.jsonl", expected[group])
            for line in lines.splitlines():
                r = json.loads(line)
                q, arm = r["id"], r["arm"]
                prior_default = directory == "preflight"
                if prior_default and (q not in controls or arm != "baseline"):
                    continue
                event = r["events"][0]
                messages = event["messages"]
                question, rubric = gold[group, q]
                first = digest(messages)
                if not prior_default and first != freeze["plan"][f"{q}/{arm}"]["message_sha"]:
                    raise ValueError(f"Archived prompt digest mismatch: {q}")
                origin = dict(
                    group=group,
                    qid=q,
                    arm=arm,
                    stage_strict=scores.get((group, q, arm), {}),
                    historical_default_control=prior_default,
                    prior_http=None if prior_default else r["http"],
                )
                binding = bindings[q]
                out[group, q, arm] = dict(
                    id=q,
                    qid=q,
                    group=group,
                    question=question,
                    gold=rubric,
                    language=rubric.get("language")
                    or ("zh" if re.search("[\u3400-\u9fff]", question) else "en"),
                    messages=messages,
                    selected=r["selected"],
                    message_sha256=first,
                    first_request_sha256=event["request_sha256"],
                    kb=binding["kb"],
                    snapshot=binding["snapshot"],
                    origins=[origin],
                )
    if len(out) != 556:
        raise ValueError("Archive input coverage changed")
    return out


def history_control_labels(repo):
    """Earlier paid default judgments keyed by (qid, stage); None means unverified."""
    repo = Path(repo)
    index = history_case_index(repo)
    ref = "85563aebcc3f9e9ee302bec951fdff71a6977c2e"
    prefix = "benchmarks/retrieval/experiments/"

    def archived(path):
        return subprocess.check_output(["git", "show", f"{ref}:{prefix}{path}"], cwd=repo)

    output = {
        (q, stage): None
        for (g, q, arm) in index
        if g in {"R", "B", "W"} and arm == "baseline"
        for stage in ("first", "final")
    }
    rlang = json.loads(archived("first_refusal_100/results/language-audit.json"))
    corrections = {r["id"]: r["baseline"] for r in rlang["mismatching_questions"]}
    for group, series, folder, reviewed in (
        ("R", "first-refusal-100-20261118", "first_refusal_100", "reviewed.jsonl"),
        ("B", "bilingual-refusal-20261118", "bilingual_refusal_100", "reviewed-rows.jsonl"),
    ):
        wire = repo / ".scratch" / series / "independent/wire-private.jsonl"
        seen = {}
        for line in wire.read_bytes().splitlines():
            r = json.loads(line)
            if r["attempt"] == 1 and r["key"].endswith("/baseline"):
                seen[r["key"].split("/")[0]] = digest(r["messages"])
        for line in archived(f"{folder}/results/{reviewed}").splitlines():
            r = json.loads(line)
            if r["arm"] != "baseline":
                continue
            q = r["id"]
            if seen[q] != index[group, q, "baseline"]["message_sha256"]:
                raise ValueError(f"Earlier paid default input changed: {q}")
            for stage in ("first", "final"):
                s = r[stage]["scores"]
                if group == "B":
                    correct = s["semantic_and_citation_correct"] and s["language_match"]
                else:
                    correct = s["joint_correct"]
                    if q in corrections:
                        correct = corrections[q][stage]["pass_with_language"]
                output[q, stage] = correct
    return output


def load_cases(repo, fresh_records=None):
    """A distinct message hash is one observation. Overlaps remain in origins."""
    history = history_case_index(repo)
    dedup = {}
    for row in history.values():
        q = row["qid"]
        origin = row["origins"][0]
        if not (
            origin["historical_default_control"]
            or any(origin["stage_strict"].values())
            or q in TARGETS
        ):
            continue
        sha = row["message_sha256"]
        if sha in dedup:
            if (
                dedup[sha]["gold"] != row["gold"]
                or dedup[sha]["first_request_sha256"] != row["first_request_sha256"]
            ):
                raise ValueError(f"Conflicting exact-input metadata: {q}")
            dedup[sha]["origins"].append(origin)
            continue
        dedup[sha] = {
            **row,
            "origins": [origin],
            "id": f"{q}-{sha[:12]}",
            "cohort": "target" if q in TARGETS else "protection",
        }
    rows = list(dedup.values())
    for row in rows:
        row["protected"] = any(
            o["historical_default_control"] or any(o["stage_strict"].values())
            for o in row["origins"]
        )
    if fresh_records is not None:
        new = fresh_sources()
        seen = {normalized(r["question"]) for r in history.values()}
        seen_text = {normalized(s["text"]) for r in history.values() for s in r["selected"]}
        records = json.loads(Path(fresh_records).read_bytes())
        short_ids = {r["id"] for r in new}
        if set(records) - short_ids:
            manifest = json.loads((FIXTURES / "long-manifest.json").read_bytes())
            long_path = Path(repo) / ".scratch/output-limit-20261119/long-sources.jsonl"
            long = [json.loads(line) for line in long_path.read_bytes().splitlines()]
            if digest(long) != manifest["derived_sha256"]:
                raise ValueError("Long independent corpus differs from preregistration")
            new.extend(long)
        if set(records) != {r["id"] for r in new}:
            raise ValueError("Missing or unexpected independent retrieval records")
        for case in new:
            short = case["id"] in short_ids
            if normalized(case["question"]) in seen or (
                short and any(normalized(d["text"]) in seen_text for d in case["documents"])
            ):
                raise ValueError("Fresh fixture duplicates exposed historical input")
            inputs = records[case["id"]]
            rows.append(
                {
                    **case,
                    **inputs,
                    "qid": case["id"],
                    "group": "independent" if short else "independent_long",
                    "cohort": "fresh" if short else "fresh_long",
                    "protected": False,
                    "origins": [],
                    "message_sha256": digest(inputs["messages"]),
                }
            )
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--fresh-records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    frozen = load_cases(args.repo, args.fresh_records)
    with args.output.open("x", encoding="utf8") as stream:
        json.dump(frozen, stream, ensure_ascii=False)
    print(
        json.dumps(
            {"cohorts": Counter(r["cohort"] for r in frozen), "freeze_sha256": digest(frozen)}
        )
    )
