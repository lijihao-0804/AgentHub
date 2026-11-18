"""Register prompts and source identities before permitting the one-shot paid comparison."""

import hashlib
import json
import subprocess

from .runner import (
    EMBED,
    FRESH,
    GROUPS,
    HISTORY,
    PREFLIGHT,
    PRIVATE,
    RERANK,
    ROOT,
    load,
    material,
    preflight_dir,
    save,
)
from .wire import CAMPAIGN_CNY, MAX_CALLS, digest, reservation

OLD = "85563aebcc3f9e9ee302bec951fdff71a6977c2e"
EXPERIMENTS = "benchmarks/retrieval/experiments/"


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def git_file(name):
    return subprocess.check_output(["git", "show", OLD + ":" + EXPERIMENTS + name], cwd=ROOT)


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def archived_goals():
    goals = {}

    def remember(qid, stage, value):
        key = goals.setdefault(qid, {"first": False, "final": False})
        key[stage] |= value is True

    for name in ("protected-100.json", "protected-wai14.json"):
        for r in json.loads(git_file("bilingual_refusal_100/results/" + name))["rows"]:
            remember(r["id"], r["stage"], r["current"] or r["previous_success"])
    for line in git_file("bilingual_refusal_100/results/reviewed-rows.jsonl").splitlines():
        r = json.loads(line)
        for stage in ("first", "final"):
            remember(
                r["id"],
                stage,
                r[stage]["scores"]["semantic_and_citation_correct"]
                and r[stage]["scores"]["language_match"],
            )
    if len(goals) != 214:
        raise ValueError("Historical denominator differs")
    return goals


def tracked_sources():
    result = {}
    for directory in (
        "packages/knowledge",
        "packages/model_gateway",
        "apps/api",
        "benchmarks/retrieval/experiments/composite_qa",
    ):
        for p in (ROOT / directory).rglob("*.py"):
            result[p.relative_to(ROOT).as_posix()] = sha(p)
    for path in (
        "packages/core/config/settings.py",
        "benchmarks/retrieval/table_header_answers.py",
        "benchmarks/retrieval/table_header_protocol.json",
        "scripts/aliyun_api_smoke.py",
    ):
        result[path] = sha(ROOT / path)
    return result


def weights():
    expected = load(HISTORY / "independent/freeze.json")["model_files_sha256"]
    for name, folder in (("embedding", EMBED), ("reranker", RERANK)):
        for filename, fingerprint in expected[name].items():
            if sha(folder / filename) != fingerprint:
                raise ValueError(f"Model weight or tokenizer mismatch {name}/{filename}")
    return expected


def audit():
    goals = archived_goals()
    plan, manifest, controls = {}, {}, []
    for group, (history, _) in GROUPS.items():
        path = preflight_dir(group) / (group + ".jsonl")
        bindings = preflight_dir(group) / (group + "-bindings.json")
        manifest[str(path.relative_to(ROOT))] = sha(path)
        manifest[str(bindings.relative_to(ROOT))] = sha(bindings)
        observed = rows(path)
        registered = {case["id"] for case in material(group)}
        if len(observed) != 2 * len(registered) or {(r["id"], r["arm"]) for r in observed} != {
            (qid, arm) for qid in registered for arm in ("baseline", "candidate")
        }:
            raise ValueError("Preflight question identities differ")
        prior = {}
        if history:
            path = HISTORY / history / "wire-private.jsonl"
            evidence_audit = load(path.parent / "execution.json")
            expected_sha = (evidence_audit.get("private_file_sha256") or evidence_audit["sha256"])[
                path.name
            ]
            if sha(path) != expected_sha:
                raise ValueError("Historical wire file changed")
            prior = {r["key"]: r["messages"] for r in rows(path) if r["attempt"] == 1}
        historical_main = prior if group == "B" else {}
        if group == "R":
            old = PRIVATE / "first-refusal-100-20261118/independent"
            wire_file = old / "wire-private.jsonl"
            if sha(wire_file) != load(old / "execution.json")["wire_sha256"]:
                raise ValueError("Archived main baseline differs from its recorded wire hash.")
            historical_main = {
                r["key"]: r["messages"] for r in rows(wire_file) if r["attempt"] == 1
            }
        off = {r["id"]: r for r in observed if r["arm"] == "baseline"}
        for row in observed:
            qid = row["id"]
            arm = row["arm"]
            key = qid + "/" + arm
            if row["http"] != 200 or len(row["events"]) != 2:
                raise ValueError("Incomplete zero-cost API correction preflight")
            first, second = row["events"]
            messages = first["messages"]
            history_messages = prior.get(qid + "/candidate")
            correction_bound = second["byte_bound"] + 4096
            allowed = correction_bound <= 32768 - 256
            if group != "fresh" and arm == "candidate":
                old_base = off[qid]["events"][0]["messages"]
                controls.append(
                    dict(
                        id=qid,
                        protected=goals[qid],
                        archived_main_messages_equal=(
                            historical_main[qid + "/baseline"] == old_base
                            if qid + "/baseline" in historical_main
                            else None
                        ),
                        archive_system_matches=history_messages[0] == messages[0],
                        archive_user_matches=history_messages[1] == messages[1],
                        archive_user_matches_main=history_messages[1] == old_base[1],
                        candidate_equals_main=old_base == messages,
                    )
                )
            selected = group == "fresh" or arm == "candidate"
            plan[key] = dict(
                planned=selected,
                send=selected and allowed,
                first_bound=first["byte_bound"],
                correction_probe_bound=second["byte_bound"],
                correction_bound=correction_bound,
                message_sha=digest(messages),
            )
        wanted = {"fresh": 48, "R": 100, "B": 100, "W": 14}[group]
        if sum(plan[k]["send"] for k in plan if k.split("/")[0] in registered) != wanted:
            raise ValueError(f"Group {group} does not admit all {wanted} registered HTTP requests")
    manifest[str(FRESH.relative_to(ROOT))] = sha(FRESH)
    paid = [k for k, v in plan.items() if v["send"]]
    maximum = sum(
        reservation(plan[k]["first_bound"]) + reservation(plan[k]["correction_bound"]) for k in paid
    )
    if 2 * len(paid) > MAX_CALLS or maximum > CAMPAIGN_CNY:
        raise ValueError("Campaign exceeds authorized maximum")
    return dict(
        schema=1,
        old_history_commit=OLD,
        model="deepseek-v4.1-flash-1017",
        code_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, cwd=ROOT
        ).strip(),
        sources=tracked_sources(),
        tariff=dict(
            region="cn-beijing",
            timezone="Asia/Shanghai",
            peak_hours="[08:00,22:00)",
            peak_cny_per_million={"miss": "2", "hit": "0.2", "output": "8"},
            offpeak_cny_per_million={"miss": "1", "hit": "0.1", "output": "4"},
            sources=[
                "https://help.aliyun.com/zh/model-studio/model-pricing",
                "https://help.aliyun.com/zh/model-studio/deepseek-v4-1-flash",
            ],
        ),
        private_artifacts=manifest,
        plan=plan,
        controls=controls,
        weights=weights(),
        max_http=len(paid),
        max_wire=len(paid) * 2,
        max_cny=str(maximum),
        initial_max_cny=str(CAMPAIGN_CNY),
    )


def verify(expected):
    frozen = load(PREFLIGHT / "freeze.json")
    if not expected or sha(PREFLIGHT / "freeze.json") != expected or audit() != frozen:
        raise ValueError("Paid dispatch requires unchanged verified campaign registration")
    return {k: r for k, r in frozen["plan"].items() if r["send"]}


if __name__ == "__main__":
    frozen = audit()
    save(PREFLIGHT / "freeze.json", frozen)
    print(
        json.dumps(
            dict(
                file_sha256=sha(PREFLIGHT / "freeze.json"),
                max_http=frozen["max_http"],
                max_wire=frozen["max_wire"],
                max_cny=frozen["max_cny"],
                archive_main_user_equal=sum(
                    r["archive_user_matches_main"] for r in frozen["controls"]
                ),
                preflight_excluded=[
                    k for k, r in frozen["plan"].items() if r["planned"] and not r["send"]
                ],
            )
        )
    )
