"""Restrict this bounded development experiment to its registered executions."""

from __future__ import annotations


def validate_execution_name(run_name, continuation_from, amendment):
    if amendment is None:
        if run_name != "live-run-1" or continuation_from is not None:
            raise ValueError("only the registered initial execution is allowed")
        return
    if run_name != amendment["new_run"] or continuation_from != amendment["original_run"]:
        raise ValueError("only the registered untouched remainder is allowed")
