"""Match actual, confirmed fresh requests to preflight and rescore their transmitted evidence."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from .freeze import HERE, OUT, as_bytes, file_hash, sha
from .score import checked, collect, coverage, first_prompt, sources_for, summarize


def physical_attempts(events):
    physical = defaultdict(dict)
    for event in events:
        key = event["key"]
        attempt = event["attempt"]
        checked(attempt in (1, 2), "Unregistered retry")
        record = physical[key].setdefault(attempt, {})
        state = event["state"]
        if state == "reserved":
            checked(not record, "Duplicate physical reservation")
        else:
            checked(state in ("confirmed", "unknown"), "Unexpected physical state")
            checked(
                set(record) == {"reserved"} and record["reserved"]["call"] == event["call"],
                "Physical settlement does not match reservation",
            )
        record[state] = event
    return physical


def compare(preflight, evidence, paid, ledger):
    offline = collect(preflight)
    frozen = json.loads(evidence.read_bytes())
    checked(frozen["receipt_sha256"] == file_hash(preflight), "Offline receipt file differs")
    checked(frozen["manifest_sha256"] == file_hash(HERE / "manifest.json"), "Manifest differs")
    cases = {
        c["id"]: c
        for c in json.loads((OUT / "matched.json").read_bytes())["cases"]
        if c["cohort"] != "smoke"
    }
    per_case = {r["id"]: r for r in frozen["evidence_pairs"]}
    checked(offline.keys() == per_case.keys() == cases.keys(), "Offline denominator differs")
    expected = {f"{qid}/{arm}" for qid in cases for arm in ("baseline", "candidate")}
    actual = collect(paid) if paid.is_file() else {}
    checked(
        all(f"{qid}/{arm}" in expected for qid, arms in actual.items() for arm in arms),
        "Unexpected paid case or arm",
    )
    ledger_snapshot = ledger.read_bytes()
    # R/B/W may still be appending after the closed fresh group. Bind the same bytes we
    # inspect and an independently reproducible stable projection of just the fresh calls.
    fresh_ledger = [
        event for event in map(json.loads, ledger_snapshot.splitlines()) if event["key"] in expected
    ]
    attempts = physical_attempts(fresh_ledger)
    results, counts, complete_pairs = [], Counter(), []
    for qid, case in cases.items():
        sources = sources_for(case)
        gold = {g["source_id"] for g in case["gold_spans"]}
        observed_arms = {}
        for arm in ("baseline", "candidate"):
            key = f"{qid}/{arm}"
            ref = offline[qid][arm]
            first_ref = ref["events"][0]
            events = attempts.get(key, {})
            observation = actual.get(qid, {}).get(arm)
            first = events.get(1, {})
            result = dict(id=qid, arm=arm)
            if not first:
                checked(not events and observation is None, "Logical row without physical attempt")
                result["state"] = "not_dispatched"
            else:
                checked(
                    first["reserved"]["request_sha256"] == first_ref["request_sha256"],
                    "Reserved first payload differs from preflight",
                )
                result["first_request_sha256"] = first_ref["request_sha256"]
                if "confirmed" not in first:
                    checked(observation is None and 2 not in events, "Unconfirmed first was reused")
                    result["state"] = "first_unknown"
                elif observation is None:
                    # A confirmed first can precede an unknown correction or a local crash.
                    # Its payload hash is checkable while the caller's logical capture is absent.
                    result["state"] = "confirmed_first_without_logical_capture"
                else:
                    result["state"] = "confirmed_with_logical_capture"
                    result["terminal_http"] = observation["http"]
                    checked(
                        len(events) == len(observation["events"]),
                        "Paid logical and physical counts differ",
                    )
                    for number, logical in enumerate(observation["events"], 1):
                        phy = events[number]
                        checked(
                            phy["confirmed"]["id"] == logical["id"]
                            and phy["reserved"]["request_sha256"] == logical["request_sha256"],
                            "Paid logical and physical receipts differ",
                        )
                    live = observation["events"][0]
                    checked(
                        all(
                            live[f] == first_ref[f]
                            for f in ("messages", "request_sha256", "parameters_sha256")
                        ),
                        "Paid serialized first request differs from preflight",
                    )
                    for field in ("ranked", "rerank_input", "deduped", "selected"):
                        checked(observation[field] == ref[field], f"Paid {field} differs")
                    actual_coverage = coverage(observation["selected"], sources, gold)
                    digest = first_prompt(observation, case)
                    registered_arm = per_case[qid][arm]
                    checked(digest == registered_arm["message_sha256"], "Scored prompt differs")
                    checked(
                        all(registered_arm[k] == v for k, v in actual_coverage.items()),
                        "Rescored live evidence differs",
                    )
                    result.update(actual_coverage, first_message_sha256=digest)
                    observed_arms[arm] = dict(registered_arm, **actual_coverage)
            if 2 in events:
                checked("confirmed" in first, "Correction followed an unconfirmed first attempt")
                result["correction_state"] = "confirmed" if "confirmed" in events[2] else "unknown"
            counts[result["state"]] += 1
            results.append(result)
        if len(observed_arms) == 2:
            complete_pairs.append(dict(per_case[qid], **observed_arms))
    primary = [r for r in complete_pairs if r["stratum"].startswith("primary_")]
    confirmed = [r for r in fresh_ledger if r["state"] == "confirmed"]
    checked(len(confirmed) == len({r["id"] for r in confirmed}), "Repeated paid receipt ID")
    return dict(
        schema=1,
        offline_receipts_sha256=file_hash(preflight),
        paid_receipts_sha256=file_hash(paid) if paid.is_file() else None,
        paid_ledger_snapshot_sha256=sha(ledger_snapshot),
        fresh_physical_ledger_events_sha256=sha(as_bytes(fresh_ledger)),
        planned_arms=len(expected),
        state_by_arm=dict(counts),
        confirmed_fresh_physical_attempts=len(confirmed),
        confirmed_fresh_corrections=sum(r["attempt"] == 2 for r in confirmed),
        terminal_http_histogram=dict(
            Counter(r["terminal_http"] for r in results if "terminal_http" in r)
        ),
        planned_primary_pairs=48,
        confirmed_primary_pairs_with_both_captures=len(primary),
        planned_negative_pairs=16,
        confirmed_negative_pairs_with_both_captures=len(complete_pairs) - len(primary),
        evidence_for_confirmed_primary_pairs=summarize(primary) if primary else None,
        arms=results,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("preflight", "offline_evidence", "paid", "ledger", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    report = compare(args.preflight, args.offline_evidence, args.paid, args.ledger)
    args.output.write_bytes(as_bytes(report))
    print(json.dumps(report["state_by_arm"]))
