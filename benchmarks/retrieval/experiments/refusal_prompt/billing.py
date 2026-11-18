"""Carry the sealed 2026-11-19 account into a two-stage prompt trial."""

import sqlite3
from contextlib import closing

from ..first_refusal_contract import billing as first
from ..first_refusal_contract.campaign import OUT as PREDECESSOR
from ..output_limit.cases import digest
from ..output_limit.meter import CAP, Meter
from ..output_limit.report import money, read_ledger
from ..output_limit.tail_recovery import lines, load

OPENING = 312094384
ACCOUNT_HASH = "ebd64ad97e393cf6be9d7a1505970af588b81093489a79a1b1b383dd27b23a36"
LEDGER_HASH = "fbff50c2193ee5aa5c989c7a87ab248f464ef3fdde3d95dcc266f2daac8ae032"
GRADES_HASH = "81b3726822b274953886521883e9587fcb03f00bdbf0e799eca3f335aa20f1f6"


def previous():
    _, tail = first.history()
    ledger = read_ledger(PREDECESSOR / "ledger.sqlite")
    report = load(PREDECESSOR / "account.json")
    freeze = load(PREDECESSOR / "freeze.json")
    outcomes = lines(PREDECESSOR / "outcomes.jsonl")
    cases, grades = (load(PREDECESSOR / p) for p in ("cases.json", "grades.json"))
    seen = first.early_ids() | ledger.keys()
    rebuilt = first.account(outcomes, tail, ledger)
    if (
        digest(report) != ACCOUNT_HASH
        or digest(ledger) != LEDGER_HASH
        or len(seen) != 1760
        or first.early_ids().intersection(ledger)
        or rebuilt != {k: v for k, v in report.items() if k not in {"outcomes_sha", "freeze_sha"}}
        or digest(cases) != freeze["inputs"]
        or digest(grades) != GRADES_HASH
        or digest(outcomes) != report["outcomes_sha"]
        or digest(freeze) != report["freeze_sha"]
        or first.CARRIED + sum(ledger[i]["cost"] for i in ledger.keys() - tail.keys()) != OPENING
    ):
        raise ValueError("Previous sealed account or registered input differs")
    return cases, outcomes, grades, ledger


class StageMeter(first.SuccessorMeter):
    def __init__(self, path, prior, *, create):
        if create:
            with path.open("xb"):
                pass
            with closing(
                sqlite3.connect(
                    (PREDECESSOR / "ledger.sqlite").resolve().as_uri() + "?mode=ro", uri=True
                )
            ) as src:
                with closing(sqlite3.connect(path)) as dest:
                    src.backup(dest)
        if read_ledger(path) != prior:
            raise ValueError("Unsettled, stale or unrelated successor receipt")
        Meter.__init__(self, path)
        self.prior_ids = first.early_ids() | prior.keys()


def close(rows, inherited, ledger, opening):
    # Prior-stage candidates may be delivered again for overlapping protection
    # inputs. Their signed paid response is separately attributed and costs zero now.
    checked = []
    prior_stage = 0
    for row in rows:
        if row["source"] == "development_candidate_replay":
            if any(e["provenance"] != "development_candidate_replay" for e in row["events"]):
                raise ValueError("Invalid prior-stage attribution")
            checked.append(
                dict(
                    row,
                    mode="archive",
                    events=[dict(e, provenance="archived_receipt") for e in row["events"]],
                )
            )
            prior_stage += len(row["events"])
        else:
            checked.append(row)
    check = first.account(checked, inherited, ledger)
    spent = sum(ledger[i]["cost"] for i in ledger.keys() - inherited.keys())
    if opening + spent > CAP:
        raise ValueError("Cumulative budget exceeded")
    return dict(
        opening_cny=money(opening),
        historical_locked_ids=1760,
        prior_unknown_locked=2,
        prior_unknown_reserved_cny="0.04906200",
        new_unknown=0,
        new_physical=check["new_physical"],
        new_cost_cny=money(spent),
        new_cost_units=spent,
        cumulative_cny=money(opening + spent),
        remaining_cny=money(CAP - opening - spent),
        prior_stage_live_candidate_invocations_replayed=prior_stage,
        new_physical_by_attempt=check["new_physical_by_attempt"],
        logical_by_receipt_origin_and_arm=check["logical_by_mode_arm"],
        outcomes_sha=digest(rows),
        ledger_sha=digest(ledger),
    )
