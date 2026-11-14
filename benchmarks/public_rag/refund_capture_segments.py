"""Combine the original refund capture and its first-dispatch continuation, offline."""

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmarks.evaluation.experiments.support_refund_rag.reviewed_dev import reviewed_cases
from benchmarks.public_rag.confirmation_cases import read, save
from benchmarks.public_rag.confirmation_receipts import reconcile

ROW_KEYS = (
    "case_id",
    "stage",
    "answerability",
    "uncited_missing_fact_review",
    "run_id",
    "thread_id",
    "contract_pass",
    "contract_checks",
    "coverage",
    "semantic_verdict",
)
FIXTURES = Path(__file__).resolve().parents[1] / "evaluation/experiments/support_refund_rag"


def check(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recover(original, path, original_hash):
    recovered = read(path)
    info = recovered["reconciliation"]
    for field in original:
        if field != "semantic_verdict":
            check(original[field] == recovered.get(field), f"038 changed original {field}")
    check(
        recovered["source_sha256"] == original_hash
        and info["no_new_dispatch"] is True
        and info["contract_status"] == "NOT_ESTABLISHED"
        and recovered["contract_pass"] is False
        and recovered["contract_checks"]["no_write"] is None
        and recovered["tickets_before"] is None
        and recovered["approvals_before"] is None
        and recovered["original_capture_verdict"] == original["semantic_verdict"],
        "038 pre-interruption contract cannot be reconstructed",
    )
    run, turn = recovered["run"], recovered["turn"]
    check(
        run["status"] == "SUCCEEDED"
        and run["thread_turn_id"] == turn["id"]
        and run["id"] == turn["agent_run_id"] == original["run_id"]
        and run["thread_id"] == turn["thread_id"] == original["thread_id"]
        and turn["user_input"] == original["expected"]["input"]
        and bool(run["final_output"])
        and original["calls"][-1]["response"]["content"] == run["final_output"],
        "038 persisted response differs from original final response or turn",
    )
    # The normal HTTP Turn view projects these two values from its linked Run.
    projected = dict(turn, final_output=run["final_output"], status=run["status"])
    snapshots = run["effective_knowledge_snapshots"]
    if isinstance(snapshots, str):
        run = dict(run, effective_knowledge_snapshots=json.loads(snapshots))
    return dict(recovered, turn=projected, run=run)


def combine(first, tail, ledger, out, recovered=None):
    expected = {
        case["case_id"]: (stage, case)
        for stage, cases in [
            ("refund_dev", reviewed_cases()),
            ("refund_new", read(FIXTURES / "new_ticket_cases.json")["cases"]),
        ]
        for case in cases
    }
    schedule = {key: stage for key, (stage, _) in expected.items()}
    keys = list(schedule)
    reports = [read(directory / "report.json") for directory in (first, tail)]
    base, continuation = reports
    check(not out.exists() and out.parent.is_dir(), "output must be a new directory")
    check(len(keys) == 52 and keys[37] == "S0-038", "unexpected frozen study schedule")
    for name in ("workspace_id", "db_name"):
        check(base[name] and base[name] == continuation[name], f"different {name}")
    check(
        continuation["prior_report_sha256"] == sha(first / "report.json")
        and continuation["original_commit"] == base["code_commit"]
        and continuation["original_python_source_sha256"] == base["python_source_sha256"],
        "continuation points to a different original execution",
    )
    if "continuation_schedule" in continuation:
        check(
            list(continuation["continuation_schedule"].items())
            == [(k, schedule[k]) for k in keys[38:]],
            "continuation dispatch plan differs from frozen suffix",
        )
    groups = dict(base["groups"])
    for stage, group in continuation["groups"].items():
        check(stage not in groups or groups[stage] == group, f"changed group {stage}")
        groups[stage] = group
    check(set(groups) == set(schedule.values()), "missing frozen group")
    for group in groups.values():
        check(
            all(group[k]["workspace_id"] == base["workspace_id"] for k in ("version", "snapshot")),
            "group workspace mismatch",
        )

    charges = reconcile(ledger)
    wires = [w for w in charges["wires"] if w["stage"] in groups]
    providers = [w["provider_id"] for w in wires if w["provider_id"]]
    check(len(providers) == len(set(providers)), "duplicate refund provider response")
    check(list(dict.fromkeys(w["task"] for w in wires)) == keys, "ledger schedule incomplete")
    check(
        [keys.index(w["task"]) for w in wires] == sorted(keys.index(w["task"]) for w in wires),
        "ledger returns to a previously dispatched case",
    )
    seen_runs, seen_threads, consumed_wires = set(), set(), []
    rows, segments = [], []
    with TemporaryDirectory(dir=out.parent) as staging:
        directory = Path(staging) / "capture"
        directory.mkdir()
        for index, (src, report, planned) in enumerate(
            zip((first, tail), reports, (keys[:38], keys[38:]), strict=True)
        ):
            check(report["mode"] == "paid", "not a paid first-dispatch segment")
            schedule_items = list(report["schedule"].items())
            check(
                schedule_items == list(schedule.items())
                or (index == 1 and schedule_items == [(k, schedule[k]) for k in planned]),
                "segment schedule differs from frozen order",
            )
            check([r["case_id"] for r in report["rows"]] == planned, "segment rows differ")
            source_cases = {p.stem for pat in ("S0-*.json", "NEW-*.json") for p in src.glob(pat)}
            check(source_cases == set(planned), "orphan or missing case receipt")
            hashes = {}
            for row in report["rows"]:
                key = row["case_id"]
                source = src / (key + ".json")
                record = read(source)
                hashes[key] = sha(source)
                stage, fixture = expected[key]
                check(
                    all(row.get(k) == record.get(k) for k in ROW_KEYS)
                    and record["stage"] == stage
                    and record["expected"] == fixture,
                    f"{key} report, stage or fixture mismatch",
                )
                rid, tid = record["run_id"], record["thread_id"]
                check(
                    rid and tid and rid not in seen_runs and tid not in seen_threads,
                    f"{key} missing or duplicate Run/Thread",
                )
                seen_runs.add(rid)
                seen_threads.add(tid)
                attempts = [w for w in wires if w["stage"] == stage and w["task"] == key]
                check(
                    attempts and record["physical_attempts"] == attempts, f"{key} ledger mismatch"
                )
                consumed_wires.extend(w["id"] for w in attempts)
                calls = record["calls"]
                check(len(calls) == len(attempts), f"{key} model call / physical count mismatch")
                for call, attempt in zip(calls, attempts, strict=True):
                    if call["response"] is not None:
                        usage = call["response"]["usage"]
                        check(
                            [usage["input_tokens"], usage["output_tokens"]]
                            == [attempt["input"], attempt["output"]],
                            f"{key} model usage differs from ledger",
                        )
                if key == "S0-038" and recovered:
                    record = recover(record, recovered, hashes[key])
                if record.get("run"):
                    run = record["run"]
                    version, snapshot = groups[stage]["version"], groups[stage]["snapshot"]
                    check(
                        run["id"] == rid == record["turn"]["agent_run_id"]
                        and record["turn"]["thread_id"] == tid
                        and run["workspace_id"] == base["workspace_id"]
                        and run["agent_version_id"] == version["id"]
                        and run["resolved_spec_hash"] == version["resolved_spec_hash"]
                        and run["effective_knowledge_snapshots"]
                        == [
                            {
                                "snapshot_id": snapshot["id"],
                                "snapshot_hash": snapshot["content_hash"],
                                "knowledge_base_id": snapshot["knowledge_base_id"],
                            }
                        ],
                        f"{key} persisted run binding mismatch",
                    )
                else:
                    check(
                        record["semantic_verdict"] == "RUN_ERROR"
                        and not record.get("contract_pass"),
                        f"{key} missing run evidence",
                    )
                rows.append({k: record.get(k) for k in ROW_KEYS})
                save(directory / (key + ".json"), record)
            segments.append(
                {
                    "source_directory": str(src),
                    "report_sha256": sha(src / "report.json"),
                    "case_sha256": hashes,
                    **{k: v for k, v in report.items() if k not in ("schedule", "groups", "rows")},
                }
            )
        check(consumed_wires == [w["id"] for w in wires], "unexpected refund ledger rows")
        commits = [s["code_commit"] for s in segments]
        report = {
            **base,
            "schedule": schedule,
            "rows": rows,
            "groups": groups,
            "code_commit": " / ".join(dict.fromkeys(commits)),
            "python_source_sha256": [s.get("python_source_sha256") for s in segments],
            "capture_segments": segments,
            "interrupted_readback": {
                "case_id": "S0-038",
                "original_verdict": base["rows"][-1]["semantic_verdict"],
                "source": str(recovered) if recovered else None,
                "reconciled_sha256": sha(recovered) if recovered else None,
            },
            "refund_ledger_stages": {stage: charges["stages"][stage] for stage in groups},
        }
        save(directory / "report.json", report)
        directory.rename(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("first", "tail", "ledger", "out"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--reconciled", type=Path)
    args = parser.parse_args()
    combine(args.first, args.tail, args.ledger, args.out, args.reconciled)


if __name__ == "__main__":
    main()
