"""Export redacted volume, usage, cost and latency counts from the sealed private batch."""

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .inputs import FORMAL, HISTORY, HISTORY_DIRS, PRIVATE, ROOT, STUDY, load, rows, sha
from .ledger import settle


def archived_first(folder):
    wire = folder / "wire-private.jsonl"
    audit = load(folder / "execution.json")
    expected = (
        audit.get("wire_sha256") or (audit.get("private_file_sha256") or audit["sha256"])[wire.name]
    )
    if sha(wire) != expected:
        raise ValueError("Archived provider messages differ from their original audit")
    return {r["key"]: r["messages"] for r in rows(wire) if r["attempt"] == 1}


def historical_messages():
    summary = {}
    for group in ("R", "B", "W"):
        candidate = archived_first(HISTORY / HISTORY_DIRS[group])
        main = (
            archived_first(PRIVATE / "first-refusal-100-20261118/independent")
            if group == "R"
            else candidate
        )
        current = rows(STUDY / "preflight" / (group + ".jsonl"))
        summary[group] = {}
        for label, arm, previous_arm, history in (
            ("baseline", "baseline", "baseline", main),
            ("candidate", "candidate", "candidate", candidate),
            ("archived_candidate_to_current_main", "baseline", "candidate", candidate),
        ):
            candidates = [r for r in current if r["arm"] == arm]
            comparisons = []
            for r in candidates:
                # The archived W live rerun registered the candidate arm only.
                prior = history.get(r["id"] + "/" + previous_arm)
                if prior is not None:
                    now = r["events"][0]["messages"]
                    comparisons.append((r["id"], prior[0] == now[0], prior[1] == now[1]))
            summary[group][label] = dict(
                checked=len(comparisons),
                system_equal=sum(bool(s) for _, s, _ in comparisons),
                user_equal=sum(bool(u) for _, _, u in comparisons),
                system_and_user_equal=sum(bool(s and u) for _, s, u in comparisons),
                user_changed_ids=[i for i, _, u in comparisons if not u],
            )
    return summary


def stage_summary(stage):
    directory = STUDY / ("paid-" + stage)
    freeze_path = STUDY / (stage + "-freeze.json")
    frozen = load(freeze_path)
    billing = settle(stage, require_complete=False)
    if load(directory / "billing.json") != billing:
        raise ValueError("Final billing differs from the raw ledger")
    raw = rows(directory / "ledger.jsonl")
    receipt_counts = Counter(r["state"] for r in raw)
    groups = {}
    for group in ("smoke",) if stage == "smoke" else FORMAL:
        path = directory / (group + ".jsonl")
        if not path.exists():
            continue
        grouped = defaultdict(list)
        for r in rows(path):
            grouped[r["arm"]].append(r)
        for arm, exchanges in sorted(grouped.items()):
            key = group + "/" + arm
            costs = billing["group_arm"][key]
            attempts = sum(len(r["events"]) for r in exchanges)
            if billing["state"] == "COMPLETE" and attempts != costs["confirmed"]:
                raise ValueError("Reported grouped attempts do not reconcile")
            times = sorted(r["elapsed_ms"] for r in exchanges)
            groups[key] = dict(
                **costs,
                logical=len(exchanges),
                http_status=dict(sorted(Counter(r["http"] for r in exchanges).items())),
                median_ms=round(statistics.median(times), 1),
                p95_nearest_rank_ms=round(times[math.ceil(0.95 * len(times)) - 1], 1),
            )
    controls = frozen["history_controls"]
    return dict(
        code_commit=frozen["code_commit"],
        freeze_sha256=sha(freeze_path),
        input_set_sha256=frozen["set_sha256"],
        private_artifact_sha256=frozen["artifacts"],
        ledger_sha256=billing["ledger_sha256"],
        max_http=frozen["max_http"],
        max_wire=frozen["max_wire"],
        max_peak_cny=frozen["max_cny"],
        billing={k: v for k, v in billing.items() if k not in ("group_arm", "ledger_sha256")},
        raw_ledger_state_counts=receipt_counts,
        current_main_equal_prior_offline_main={
            g: dict(
                checked=sum(r["id"].startswith(g) for r in controls),
                equal=sum(
                    r["id"].startswith(g) and r["current_default_equal_archived_default"]
                    for r in controls
                ),
            )
            for g in ("R", "B", "W")
            if controls
        },
        groups=groups,
    )


def report():
    smoke, formal = stage_summary("smoke"), stage_summary("formal")
    return dict(
        schema=1,
        model="deepseek-v4.1-flash-1017",
        region="cn-beijing",
        main_baseline=load(STUDY / "formal-freeze.json")["main"],
        manifest_sha256=sha(
            ROOT / "benchmarks/public_rag/experiments/joint_six_20261118/manifest.json"
        ),
        formal_snapshot_rates=load(STUDY / "formal-freeze.json")["tariff"],
        archived_live_vs_current_preflight_first_messages=historical_messages(),
        latency_unit=(
            "Milliseconds from product API dispatch to response, "
            "including local retrieval and provider generation."
        ),
        smoke=smoke,
        formal=formal,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    summary = report()
    if args.save:
        with Path(__file__).with_name("execution.json").open("x", encoding="utf8") as out:
            json.dump(summary, out, indent=2)
            out.write("\n")
    else:
        print(json.dumps(summary, indent=2))
