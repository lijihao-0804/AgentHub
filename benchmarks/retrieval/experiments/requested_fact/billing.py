"""Roll forward the two closed trials without mutating any earlier budget."""

import sqlite3
from contextlib import closing

from ..first_refusal_contract import billing as first
from ..output_limit.cases import digest
from ..output_limit.meter import Meter
from ..output_limit.report import read_ledger
from ..output_limit.tail_recovery import lines, load
from ..refusal_prompt import billing as second
from ..refusal_prompt.campaign import OUT as LAST

OPENING = 326662012
ACCOUNT = "0c6042cd77ee01d8b5ce00e1183689b58337ef7de66ca3e4bf21d5ba3d2deed5"
GRADES = "bd4ddd35267cd91d98d6c0f4727cd38f412a3469c8896b357916b1ceb208a351"


def opening():
    one_cases, one_rows, one_grades, one_ledger = second.previous()
    directory = LAST / "development"
    report = load(directory / "account.json")
    cases, grades = (load(directory / p) for p in ("cases.json", "grades.json"))
    rows = lines(directory / "outcomes.jsonl")
    ledger = read_ledger(LAST / "ledger.sqlite")
    reg = load(LAST / "freeze.json")
    rebuilt = second.close(rows, one_ledger, ledger, second.OPENING)
    if (
        dict(rebuilt, freeze_sha=digest(reg)) != report
        or digest(report) != ACCOUNT
        or digest(grades) != GRADES
        or digest(cases) != reg["cases"]["development"]
        or len(first.early_ids() | ledger.keys()) != 1875
        or first.early_ids().intersection(ledger)
        or len(ledger) - len(one_ledger) != 115
        or second.OPENING + rebuilt["new_cost_units"] != OPENING
    ):
        raise ValueError("Earlier sealed account or grade binding changed")
    return (one_cases, one_rows, one_grades), (cases, rows, grades), ledger


class FactMeter(first.SuccessorMeter):
    def __init__(self, path, inherited):
        with path.open("xb"):
            pass
        with closing(
            sqlite3.connect((LAST / "ledger.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
        ) as src:
            with closing(sqlite3.connect(path)) as dest:
                src.backup(dest)
        if read_ledger(path) != inherited:
            raise ValueError("Successor account failed to carry exact sealed history")
        Meter.__init__(self, path)
        self.prior_ids = first.early_ids() | inherited.keys()


def close(rows, old, ledger):
    result = second.close(rows, old, ledger, OPENING)
    result["historical_locked_ids"] = 1875
    return result
