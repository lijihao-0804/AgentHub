"""Anonymous first-answer review and paired reporting, including failures and incomplete plans."""

import argparse
import json
import re
import secrets
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from benchmarks.evaluation.experiments.support_refund_rag.citation_contract import (
    citation_identity_audit,
)
from benchmarks.public_rag.answer_pair_freeze import ARMS, OUT, cases, file_sha, load, verify
from benchmarks.public_rag.confirmation_cases import save
from benchmarks.public_rag.confirmation_scoring import paired, percentile

AXES = (
    "correct_content",
    "conditions_complete",
    "visible_support",
    "judgment_appropriate",
    "unwarranted_refusal",
    "unsupported_fact",
    "unsupported_capability",
    "unsupported_policy",
    "contract_consistent",
    "withheld",
    "all_policy_claims_cited",
)


def observed(root):
    with closing(
        sqlite3.connect(f"{(root / 'ledger.sqlite').resolve().as_uri()}?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        attempts = {r["task"]: dict(r) for r in db.execute("SELECT * FROM attempts")}
        wires = [dict(r) for r in db.execute("SELECT * FROM wires ORDER BY id")]
        if db.execute("SELECT manifest FROM study").fetchone()[0] != file_sha(OUT / "freeze.json"):
            raise ValueError("Study identity changed")
    rows = {}
    for chain, c, arm in cases():
        key = f"{chain}/{c['case_id']}/{arm}"
        a = attempts.get(key)
        path = root / key / "first.json"
        if (root / key / "first-reconciled.json").exists():
            path = root / key / "first-reconciled.json"
        if a and a["state"] == "complete":
            if file_sha(path) != a["receipt"]:
                raise ValueError("First-answer receipt changed")
            r = load(path)
            if r["task"] != key or r["physical_receipts"] != [w for w in wires if w["task"] == key]:
                raise ValueError("Run and wire receipts disagree")
            rows[key] = r
        elif a:
            partial = {}
            if path.is_file():
                partial = load(path)
                if partial.get("task") != key:
                    raise ValueError("Unknown attempt and first-answer record disagree")
            elif chain == "support" and (root / key / "run.json").is_file():
                partial = load(root / key / "run.json")
                if partial.get("case_id") != c["case_id"] or partial.get("arm") != arm:
                    raise ValueError("Unknown attempt and run checkpoint disagree")
            rows[key] = {**partial, "status": "unknown", "terminal_known": False}
        else:
            rows[key] = {"status": "not_dispatched"}
    if not attempts.keys() <= rows.keys() or any(w["task"] not in attempts for w in wires):
        raise ValueError("Orphan task or model invocation")
    return rows, wires


def case_card(chain, c, r):
    support = chain == "support"
    if support:
        turn = r.get("turn", {})
        answer = None if r.get("status") == "unknown" else turn.get("final_output")
        audits = r.get("request_audit", [])
        visible = audits[-1]["evidence"] if audits else []
        calls = r.get("calls", [])
        last_response = calls[-1].get("response") if calls and not answer else None
        review_text = turn.get("final_output") or (last_response or {}).get("content")
        ids = {
            v["id"]: v["visible"]
            for v in citation_identity_audit(review_text, visible)["identities"]
        }
        tool_messages = (
            [m for m in calls[-1]["request"]["messages"] if m["role"] == "tool"] if calls else []
        )
        reads = {
            t["tool_identity"]
            for step in r.get("steps", [])
            for t in step["safe_metadata"].get("tool_results", [])
            if t.get("status") == "SUCCESS"
        }
        decisions = [
            [v["decision_status"], v["execution_status"]] for v in r.get("approvals_after", [])
        ]
        before = r.get("tickets_before", {}).get("total")
        after = r.get("tickets_after", {}).get("total")
        goal = c.get("decision")
        diff = {t["id"] for t in r.get("tickets_after", {}).get("items", [])} - {
            t["id"] for t in r.get("tickets_before", {}).get("items", [])
        }
        expected = {
            "approve": [["APPROVED", "SUCCEEDED"]],
            "deny": [["DENIED", "NOT_STARTED"]],
            None: [],
        }
        write_ok = (
            before is not None
            and after == before + int(goal == "approve")
            and decisions == expected[goal]
        )
        if goal == "approve":
            write_ok = (
                write_ok
                and len(diff) == 1
                and any(
                    t["id"] in diff
                    and t["subject"] == c["subject"]
                    and t["ticket_ref"] in (answer or "")
                    for t in r.get("ticket_details", [])
                )
            )
        else:
            write_ok = write_ok and not diff
        machine = {
            "run_status": "unknown"
            if r.get("status") == "unknown"
            else r.get("run", {}).get("status", r.get("status")),
            "required_reads_met": set(c["required_reads"]) <= reads,
            "write_ok": bool(write_ok),
        }
        state = {
            "visible": visible,
            "tools": tool_messages,
            "approval_execution": decisions,
            "ticket_before": before,
            "ticket_after": after,
            "ticket_details": r.get("ticket_details", []),
            "tool_schemas": calls[-1]["request"]["tools"] if calls else [],
            "last_response": last_response,
        }
        if not answer and turn.get("final_output"):
            state["unconfirmed_turn_output"] = turn["final_output"]
        if not answer and len(calls) > 1:
            state["earlier_responses"] = [
                {"round": i + 1, "response": call["response"]}
                for i, call in enumerate(calls[:-1])
                if call.get("response") is not None
            ]
    else:
        visible = r.get("evidence", [])
        answer = None if r.get("status") == "unknown" else r.get("answer")
        message = (r.get("model_events") or [{}])[-1].get("raw_message") or {}
        raw = message.get("content")
        numbers = set(r.get("citations", []))
        if not answer and isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            prose = raw
            if isinstance(payload, dict):
                prose = payload.get("answer")
                declared = payload.get("citation_ids")
                if isinstance(declared, list):
                    numbers.update(i for i in declared if type(i) is int)
            else:
                # A complete numeric citation_ids array can survive invalid outer JSON.
                pattern = r'"citation_ids"\s*:\s*\[\s*(-?\d+(?:\s*,\s*-?\d+)*)\s*\]'
                for match in re.finditer(pattern, raw):
                    numbers.update(int(i) for i in match[1].split(","))
                prose = re.sub(pattern, "", raw)
            if isinstance(prose, str):
                numbers.update(int(i) for i in re.findall(r"\[(-?\d+)\]", prose))
        visible_numbers = {v["number"] for v in visible}
        ids = {str(i): i in visible_numbers for i in sorted(numbers)}
        machine = {"run_status": r.get("status")}
        state = {
            "visible": visible,
            "insufficient_evidence": r.get("insufficient_evidence"),
            "raw_generation": raw,
        }
        if not answer and raw:
            state["citation_review_note"] = (
                "Check each listed ID against the claim in the raw generation. "
                "Describe unnumbered, ambiguous or unparsed references in the review reason."
            )
    return {
        "input": c["input"],
        "stratum": c["stratum"],
        "rubric": c["rubric"],
        "reference_documents": c.get("full_source", c["documents"]),
        "answer": answer,
        "cited_id_locatable": ids,
        "machine": machine,
        **state,
    }


def export(root, dest):
    verify()
    rows, _ = observed(root)
    dest.mkdir(parents=True, exist_ok=False)
    mapping = {}
    cards = []
    for chain, c, arm in cases():
        key = f"{chain}/{c['case_id']}/{arm}"
        if rows[key].get("status") == "not_dispatched":
            continue
        token = secrets.token_hex(16)
        mapping[token] = key
        cards.append({"token": token, **case_card(chain, c, rows[key])})
    secrets.SystemRandom().shuffle(cards)
    save(dest / "cards.json", cards)
    save(
        dest / "private.json",
        {
            "tasks": mapping,
            "cards_sha256": file_sha(dest / "cards.json"),
            "manifest": file_sha(OUT / "freeze.json"),
        },
    )


def win(chain, card, v):
    m = card["machine"]
    passed = (
        m["run_status"] == "ok"
        if chain == "citation"
        else m["run_status"] == "SUCCEEDED" and m["required_reads_met"] and m["write_ok"]
    )
    return int(
        bool(card["answer"])
        and passed
        and all(
            v[k]
            for k in (
                "correct_content",
                "conditions_complete",
                "visible_support",
                "judgment_appropriate",
                "contract_consistent",
                "all_policy_claims_cited",
            )
        )
        and not any(
            v[k]
            for k in (
                "unwarranted_refusal",
                "unsupported_fact",
                "unsupported_capability",
                "unsupported_policy",
            )
        )
        and all(card["cited_id_locatable"].values())
        and all(v["citation_support"].values())
    )


def delivery(record, chain):
    if chain == "citation":
        return {
            "user_context_sha256": record.get("request_sha256"),
            "system_tokens": record.get("system_tokens"),
            "chunks": [v["chunk_id"] for v in record.get("evidence", [])],
        }
    audits = record.get("request_audit", [])
    return {
        "rounds": len(audits),
        "extension_tokens": sum(a["support_system_extension_tokens"] for a in audits),
        "observation_tokens": sum(a["observation_field_tokens"] for a in audits),
        "final_policy_evidence": [
            (v["source_id"], v["snippet_sha256"])
            for v in (audits[-1]["evidence"] if audits else [])
        ],
    }


def report(root, dest, votes_path, output):
    verify()
    rows, wires = observed(root)
    private = load(dest / "private.json")
    cards = {r["token"]: r for r in load(dest / "cards.json")}
    votes = load(votes_path)
    if (
        private["cards_sha256"] != file_sha(dest / "cards.json")
        or private["manifest"] != file_sha(OUT / "freeze.json")
        or set(votes) != set(cards)
        or set(cards) != set(private["tasks"])
    ):
        raise ValueError("Incomplete review or changed blind packet")
    tokens = {k: t for t, k in private["tasks"].items()}
    if set(tokens) != {k for k, r in rows.items() if r.get("status") != "not_dispatched"}:
        raise ValueError("Review packet differs from the current set of dispatched tasks")
    result = {}
    for chain in ("citation", "support"):
        pairs = {}
        groups = {arm: Counter(planned=16) for arm in ARMS}
        durations = {a: [] for a in ARMS}
        failures = []
        for typ, c, arm in cases():
            if typ != chain:
                continue
            key = f"{typ}/{c['case_id']}/{arm}"
            r = rows[key]
            g = groups[arm]
            score = None
            if key in tokens:
                token = tokens[key]
                v = votes[token]
                card = cards[token]
                actual = case_card(chain, c, r)
                if {k: i for k, i in card.items() if k != "token"} != actual or (
                    any(type(v.get(k)) is not bool for k in AXES)
                    or not v.get("reason")
                    or set(v["citation_support"]) != set(card["cited_id_locatable"])
                    or any(type(b) is not bool for b in v["citation_support"].values())
                ):
                    raise ValueError("Missing judgment, mismatched receipt or citation")
                g["dispatched"] += 1
                g["completed"] += int(bool(r.get("terminal_known")))
                g["final_answer"] += int(bool(card["answer"]))
                g["missing_answer"] += int(not card["answer"])
                g["contract_failures"] += int(not v["contract_consistent"])
                g["source_citations"] += len(v["citation_support"])
                g["located"] += sum(card["cited_id_locatable"].values())
                g["supported"] += sum(
                    supported and card["cited_id_locatable"][key]
                    for key, supported in v["citation_support"].items()
                )
                for k in (
                    "unsupported_fact",
                    "unsupported_capability",
                    "unsupported_policy",
                    "unwarranted_refusal",
                    "withheld",
                ):
                    g[k] += v[k]
                if c["should_withhold"]:
                    g["should_withhold"] += 1
                    g["correct_withholding"] += int(
                        v["judgment_appropriate"] and v["withheld"] and bool(card["answer"])
                    )
                else:
                    g["should_answer"] += 1
                    g["unwarranted_on_answerable"] += v["unwarranted_refusal"]
                if "required_reads_met" in card["machine"]:
                    for k in ("required_reads_met", "write_ok"):
                        g[k] += card["machine"][k]
                if "elapsed_seconds" in r:
                    durations[arm].append(r["elapsed_seconds"])
                score = win(chain, card, v)
                if chain == "citation" and c.get("availability") == "source_only" and v["withheld"]:
                    score = 0
                g["correct"] += score
                if not score:
                    failures.append(
                        {"task": key, "cause": v["reason"], "status": card["machine"]["run_status"]}
                    )
            row = pairs.setdefault(
                c["case_id"], {"id": c["case_id"], "source": c["case_id"], "arms": {}}
            )
            row["arms"][arm] = {"quality": score, "delivery": delivery(r, chain)}
        for arm, g in groups.items():
            w = [v for v in wires if v["stage"] == chain and v["task"].endswith("/" + arm)]
            returned = [v for v in w if v["status"] == "returned"]
            g.update(
                {
                    "posts_confirmed": len(returned),
                    "unsettled": len(w) - len(returned),
                    "input_tokens": sum(v["input"] for v in returned),
                    "output_tokens": sum(v["output"] for v in returned),
                    "peak_microcny": sum(v["cost"] for v in returned),
                    "uncertain_reserve_microcny": sum(
                        v["cost"] for v in w if v["status"] != "returned"
                    ),
                }
            )
        fractions = {
            "complete_first_answer": ("correct", "dispatched"),
            "appropriate_withholding": ("correct_withholding", "should_withhold"),
            "wrong_withholding_on_answerable": ("unwarranted_on_answerable", "should_answer"),
            "citation_location": ("located", "source_citations"),
            "citation_claim_support": ("supported", "source_citations"),
        }
        result[chain] = {
            "arms": groups,
            "percentages": {
                arm: {
                    name: {
                        "numerator": g[n],
                        "denominator": g[d],
                        "percent": round(100 * g[n] / g[d], 4) if g[d] else None,
                    }
                    for name, (n, d) in fractions.items()
                }
                for arm, g in groups.items()
            },
            "regressed_cases": [
                row["id"]
                for row in pairs.values()
                if row["arms"]["baseline"]["quality"] == 1
                and row["arms"]["candidate"]["quality"] == 0
            ],
            "p50_p95_seconds": {
                a: [percentile(d, 0.5), percentile(d, 0.95)] for a, d in durations.items()
            },
            "pairs": list(pairs.values()),
            "paired": paired(pairs.values(), "candidate", "baseline", "quality"),
            "failures": failures,
            "unstarted": [
                k
                for k, v in rows.items()
                if k.startswith(chain + "/") and v.get("status") == "not_dispatched"
            ],
        }
    save(output, result)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("export", "report"))
    p.add_argument("run", type=Path)
    p.add_argument("blind", type=Path)
    p.add_argument("--votes", type=Path)
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    if a.mode == "export":
        export(a.run, a.blind)
    elif a.votes and a.output:
        report(a.run, a.blind, a.votes, a.output)
    else:
        p.error("report requires --votes and --output")
