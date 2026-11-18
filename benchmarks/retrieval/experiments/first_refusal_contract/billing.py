"""Successor billing ledger and verified history for same-wire paired QA."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from ..output_limit.cases import digest
from ..output_limit.meter import CAP, OPENING, Meter, historical_ids
from ..output_limit.report import money, read_ledger
from ..output_limit.tail_recovery import DIRECTORY, PREFIX, TAIL, lines, load, main_seal, reconcile

OLD_LEDGER = TAIL / "ledger.sqlite"
CARRIED = 300814688
TAIL_HASH = "b4c4656d4f725da67f8c460092fa87ae7171c2452ed751bdbe3f7aed90550584"
EARLY_HASH = "9884a2bc25e325d925ad2ea4dcd9758e617096a7f86b888f2516d0a512f660e9"


def early_ids():
    # The prior output-limit checker omitted the first W3C run and two WAI cohorts.
    scratch = DIRECTORY.parent
    ids = set(historical_ids())
    for relative, count in (
        ("merge-qa-20261118/once.jsonl", 28),
        ("merge-qa-20261118/public-route-once.jsonl", 35),
        ("first-refusal-100-20261118/wai-baseline/receipts.jsonl", 6),
        ("first-refusal-100-20261118/wai-candidate/receipts.jsonl", 14),
    ):
        rows = lines(scratch / relative)
        observed = {r["id"] for r in rows if r["state"] == "confirmed"}
        if len(rows) != 2 * count or len(observed) != count or ids.intersection(observed):
            raise ValueError("Earlier physical receipts missing or repeated")
        ids.update(observed)
    if len(ids) != 1039 or digest(sorted(ids)) != EARLY_HASH:
        raise ValueError("Historical ID set changed")
    for name, expected in (
        (
            "rag-table-wai-20261118/generated-once.jsonl",
            "b448503dd0d1eb6758736d50ac3d3d8ef4c511b7516d65a4697205bf77fa59d4",
        ),
        (
            "composite-qa-paid/ledger-freshRBW.jsonl",
            "e3ffc69de21b0a2d9584dbd1457ab4484d34ba74b8fc8f03b652736092b8f6f5",
        ),
    ):
        if hashlib.sha256((scratch / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Unresolved historical reservation changed")
    return ids


def charge(body, cap):
    u = body["usage"]
    i, o, c = (
        u["prompt_tokens"],
        u["completion_tokens"],
        u["prompt_tokens_details"]["cached_tokens"],
    )
    if (
        any(type(v) is not int for v in (i, o, c, u["total_tokens"], body["created"]))
        or i <= 0
        or not 0 <= c <= i
        or not 0 <= o <= cap
        or i + o != u["total_tokens"]
    ):
        raise ValueError("Provider token usage does not reconcile")
    hour = datetime.fromtimestamp(body["created"], timezone(timedelta(hours=8))).hour
    return ((i - c) * 100 + c * 2 + o * 400) * (2 if 8 <= hour < 22 else 1)


def history():
    _, rows, main = main_seal()
    tail = read_ledger(OLD_LEDGER)
    plan = load(TAIL / "freeze-8192.json")
    if (
        digest(tail) != TAIL_HASH
        or digest(plan) != "761a4326d42e2100f7587805068464e69cab026cabf56f376d3f891e30f76253"
        or plan["main_ledger"] != digest(main)
        or plan["main_outcomes"] != digest(rows)
        or {i: tail[i] for i in main} != main
        or reconcile(plan["pairs"], lines(TAIL / "outcomes-8192.jsonl"), tail, PREFIX)
        != tail.keys() - main.keys()
        or len(tail) != 624
        or early_ids().intersection(tail)
        or any(r["cost"] != charge(json.loads(r["response"]), r["cap"]) for r in tail.values())
        or OPENING + sum(r["cost"] for r in tail.values()) != CARRIED
    ):
        raise RuntimeError("Cannot verify the sealed 624-receipt account")
    return rows, tail


class SuccessorMeter(Meter):
    def __init__(self, path, old):
        if not path.exists():
            with path.open("xb"):
                pass
            with closing(
                sqlite3.connect(OLD_LEDGER.resolve().as_uri() + "?mode=ro", uri=True)
            ) as src:
                with closing(sqlite3.connect(path)) as dest:
                    src.backup(dest)
        rows = read_ledger(path)
        if {i: rows[i] for i in old} != old or any(
            not r["key"].startswith("paired/") for i, r in rows.items() if i not in old
        ):
            raise ValueError("Successor ledger contains unexpected calls")
        super().__init__(path)
        self.prior_ids = early_ids() | old.keys()

    def settle(self, key, response):
        # Fail closed (durable reservation) on an unknown provider/choice shape.
        if response.get("object") != "chat.completion" or not (
            isinstance(response.get("choices"), list)
            and len(response["choices"]) == 1
            and response["choices"][0].get("finish_reason") in {"stop", "length"}
        ):
            raise ValueError("NEEDS_ATTENTION: unexpected provider completion")
        charge(response, 2048)
        return super().settle(key, response)


def account(outcomes, old, ledger):
    new = {i: r for i, r in ledger.items() if i not in old}
    if {i: ledger[i] for i in old} != old:
        raise ValueError("Prior sealed rows changed")
    touched, archived, sent = set(), set(), set()
    breakdown = {}
    by_wire = {}
    for row in outcomes:
        events = row["events"]
        if row["cap"] != 2048 or not 1 <= len(events) <= 2:
            raise ValueError("Unexpected exchange")
        group = breakdown.setdefault(
            row["mode"] + "/" + row["arm"],
            {
                "logical": 0,
                "logical_model_calls": 0,
                "corrections": 0,
                "finish_length": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
                "cost_units": 0,
            },
        )
        group["logical"] += 1
        group["corrections"] += len(events) - 1
        for n, event in enumerate(events, 1):
            rid = event["id"]
            r = ledger[rid]
            body = json.loads(r["response"])
            choice = body["choices"][0]
            if (
                (r["key"], r["qid"], r["cap"], r["digest"])
                != (event["physical_key"], row["qid"], 2048, event["messages_sha256"])
                or (
                    body["id"],
                    body["created"],
                    body["usage"],
                    choice["message"]["content"],
                    choice["finish_reason"],
                )
                != (rid, event["created"], event["usage"], event["content"], event["finish"])
                or charge(body, 2048) != r["cost"]
            ):
                raise ValueError("Event/physical receipt mismatch")
            if row["mode"] == "archive":
                if rid not in old or event["provenance"] != "archived_receipt":
                    raise ValueError("Unknown archive source")
                archived.add(rid)
            else:
                expected = f"paired/{row['id']}/2048/{n}/{event['request_sha256']}"
                if rid not in new or expected != r["key"]:
                    raise ValueError("Unmatched physical wire")
                if by_wire.setdefault(expected, rid) != rid:
                    raise ValueError("Multiple physical receipts for a single shared wire")
                if event["provenance"] == "provider":
                    if rid in sent:
                        raise ValueError("Reported more than one dispatch for the same receipt")
                    sent.add(rid)
                elif event["provenance"] != "confirmed_reuse":
                    raise ValueError("Unknown live event source")
                touched.add(rid)
            u = event["usage"]
            group["cost_units"] += r["cost"]
            group["input_tokens"] += u["prompt_tokens"]
            group["cached_tokens"] += u["prompt_tokens_details"]["cached_tokens"]
            group["output_tokens"] += u["completion_tokens"]
            group["logical_model_calls"] += 1
            group["finish_length"] += int(event["finish"] == "length")
    if set(new) != touched or sent != touched:
        raise ValueError("Unreferenced paid receipt")
    cost = sum(r["cost"] for r in new.values())
    if CARRIED + cost > CAP:
        raise ValueError("Registered cumulative budget exceeded")
    for g in breakdown.values():
        g["logical_model_cost_cny"] = money(g.pop("cost_units"))
    by_attempt = {}
    for r in new.values():
        n = r["key"].rsplit("/", 2)[1]
        g = by_attempt.setdefault(n, {"count": 0, "cost_units": 0, "finish_length": 0})
        g["count"] += 1
        g["cost_units"] += r["cost"]
        g["finish_length"] += int(
            json.loads(r["response"])["choices"][0]["finish_reason"] == "length"
        )
    return {
        "historical_unique_ids": 1663,
        "prior_unknown_locked": 2,
        "opening_cny": money(CARRIED),
        "prior_confirmed_cny": "2.95908488",
        "prior_unknown_reserved_cny": "0.04906200",
        "prior_receipts_replayed_unique": len(archived),
        "new_physical": len(new),
        "new_physical_by_attempt": by_attempt,
        "new_cost_cny": money(cost),
        "new_confirmed_cached_uses": sum(
            g["logical_model_calls"] for k, g in breakdown.items() if k.startswith("live/")
        )
        - len(new),
        "new_unknown": 0,
        "cumulative_cny": money(CARRIED + cost),
        "remaining_cny": money(CAP - CARRIED - cost),
        "logical_by_mode_arm": breakdown,
    }
