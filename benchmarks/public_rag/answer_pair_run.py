"""First-dispatch scheduler for the frozen answer-contract comparison."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from benchmarks.public_rag.answer_pair_freeze import (
    CAP,
    LIMITS,
    OUT,
    RUN_DIR,
    cases,
    file_sha,
    freeze,
    load,
    verify,
)
from benchmarks.public_rag.confirmation_cases import save
from benchmarks.public_rag.confirmation_meter import (
    INPUT,
    AliyunMeter,
    init_ledger,
    input_bound,
    text_counter,
)
from benchmarks.public_rag.consumer_replay import CONTEXT
from benchmarks.public_rag.product_confirmation import Gateway, plan
from packages.agent_runtime.context_budget import ContextCategory
from packages.knowledge import citation_qa as qa
from packages.knowledge.contracts import RetrievedEvidence
from packages.model_gateway.contracts import ModelMessage, ModelRequest


class StudyMeter(AliyunMeter):
    limits = LIMITS

    async def acompletion(self, **kwargs):
        # Tool-capable Agent calls inherit PR19's provider thinking mode. Both
        # arms must submit identical provider options; QA explicitly disables it.
        thinking = {"enable_thinking": False} if self.stage == "citation" else None
        try:
            if (
                kwargs.get("extra_body") != thinking
                or kwargs.get("temperature") != 0
                or kwargs.get("max_tokens") != 2048
            ):
                raise ValueError("Provider options differ from the frozen arm")
            verify()
            with closing(sqlite3.connect(self.path)) as db:
                active = db.execute(
                    "SELECT state FROM attempts WHERE task=?", (self.task_id,)
                ).fetchone()
                if active != ("active",) or db.execute("SELECT 1 FROM stops").fetchone():
                    raise RuntimeError("Study stopped or task unclaimed")
            return await super().acompletion(**kwargs)
        except Exception as exc:
            # The Agent may turn an adapter exception into a terminal FAILED Run.
            # Persist the stop independently of that business outcome.
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute(
                    "INSERT INTO stops(task,reason) VALUES (?,?)",
                    (self.task_id, type(exc).__name__),
                )
            raise


def identity():
    return file_sha(OUT / "freeze.json")


def initialize(root, approval):
    verify()
    root.mkdir(parents=True, exist_ok=False)
    path = root / "ledger.sqlite"
    init_ledger(path, approval_id=approval, cap_microcny=CAP)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE study (manifest TEXT PRIMARY KEY);
            CREATE TABLE attempts (task TEXT PRIMARY KEY, state TEXT NOT NULL, receipt TEXT);
            CREATE TABLE stops(task TEXT, reason TEXT NOT NULL);
        """)
        db.execute("INSERT INTO study VALUES (?)", (identity(),))


def wires(db, key):
    db.row_factory = sqlite3.Row
    return [dict(v) for v in db.execute("SELECT * FROM wires WHERE task=? ORDER BY id", (key,))]


def claim(path, key):
    # Global serialization also covers the pre-wire and after-wire crash windows.
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT manifest FROM study").fetchone() != (identity(),):
            raise ValueError("This is not the authorized ledger for this freeze")
        if db.execute("SELECT 1 FROM stops").fetchone():
            raise RuntimeError("A dispatch or admission stopped the study")
        if db.execute("SELECT 1 FROM wires WHERE status!='returned' LIMIT 1").fetchone():
            raise RuntimeError("Unknown dispatch or usage: stop")
        if db.execute("SELECT 1 FROM attempts WHERE state!='complete' LIMIT 1").fetchone():
            raise RuntimeError("Unreconciled scheduled task; never repeat it")
        db.execute("INSERT INTO attempts(task,state) VALUES (?,'active')", (key,))


def finish(path, key, record_file):
    r = load(record_file)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        observed = wires(db, key)
        if key.startswith("citation/"):
            sdk = [
                (
                    e.get("id"),
                    (e.get("usage") or {}).get("prompt_tokens"),
                    (e.get("usage") or {}).get("completion_tokens"),
                )
                for e in r.get("model_events", [])
            ]
        else:
            sdk = [
                (e.get("provider_id"), e.get("input"), e.get("output"))
                for e in r.get("provider_receipts", [])
            ]
        if (
            not r.get("terminal_known")
            or r["task"] != key
            or not observed
            or any(w["status"] != "returned" for w in observed)
            or r["physical_receipts"] != observed
            or sdk != [(w["provider_id"], w["input"], w["output"]) for w in observed]
        ):
            raise RuntimeError("Missing terminal receipt or known usage: stop")
        n = db.execute(
            "UPDATE attempts SET state='complete',receipt=? WHERE task=? AND state='active'",
            (file_sha(record_file), key),
        ).rowcount
        if n != 1:
            raise ValueError("No unique active first attempt")


def citation_request(case, arm, contexts):
    rows = contexts[case["case_id"]]["visible"]
    selected = tuple(
        qa._NumberedEvidence(
            r["number"],
            RetrievedEvidence(**{k: v for k, v in r.items() if k != "number"}),
            r["text"],
        )
        for r in rows
    )
    if not 1 <= len(selected) <= 6 or [x.id for x in selected] != list(range(1, len(selected) + 1)):
        raise ValueError("Invalid frozen visible context")
    sys = qa.citation_system_prompt("PROMPT_JSON", answer_contract_v1=arm == "candidate")
    messages = (
        ModelMessage("system", sys),
        ModelMessage("user", qa._evidence_prompt(case["input"], selected)),
    )
    qa._deny_oversized(
        plan(2048),
        messages,
        (ContextCategory.SYSTEM_PROMPT, ContextCategory.RAG_EVIDENCE),
        response_schema=None,
    )
    return ModelRequest(messages=messages), rows


async def citation(case, arm, meter, contexts):
    request, rows = citation_request(case, arm, contexts)
    gateway = Gateway(meter.path, meter.stage, meter.task_id, [], meter=meter)
    try:
        res = await gateway.generate_resolved(CONTEXT, plan(2048), request)
        verdict = qa.resolve_citation_answer(
            case["input"],
            res.content,
            visible_ids={r["number"] for r in rows},
            output_limit_reached=res.finish_reason == "length",
            answer_contract_v1=arm == "candidate",
        )
        result = {
            "answer": verdict.answer,
            "citations": list(verdict.citation_ids),
            "insufficient_evidence": verdict.insufficient_evidence,
            "status": "ok",
            "normalization_rule": verdict.rule,
            "raw_sha256": verdict.raw_sha256,
        }
    except (qa._InvalidCitationAnswer, qa.ModelGatewayError) as exc:
        result = {
            "status": exc.reason.value
            if isinstance(exc, qa._InvalidCitationAnswer)
            else exc.code.value
        }
    events = gateway.events
    return {
        **result,
        "model_events": events,
        "evidence": rows,
        "terminal_known": bool(events and events[-1].get("id")),
        "system_tokens": text_counter().count_serialized_text(request.messages[0].content),
        "request_sha256": hashlib.sha256(request.messages[-1].content.encode()).hexdigest(),
    }


async def run(root):
    f = verify()
    path = root / "ledger.sqlite"
    contexts = load(OUT / "citation-contexts.json")
    from benchmarks.evaluation.experiments.support_refund_rag.answer_contract_pair import (
        recover_case,
        run_case,
    )

    for (chain, case, arm), key in zip(cases(), f["schedule"], strict=True):
        folder = root / key
        receipt = folder / "first.json"
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True)) as db:
            prior = db.execute("SELECT state,receipt FROM attempts WHERE task=?", (key,)).fetchone()
            physical = wires(db, key) if prior else []
        if prior:
            recovered = folder / "first-reconciled.json"
            if recovered.exists():
                receipt = recovered
            if (
                prior[0] == "active"
                and chain == "support"
                and physical
                and all(w["status"] == "returned" for w in physical)
                and (not receipt.exists() or not load(receipt).get("terminal_known"))
            ):
                record = await recover_case(case, arm, output=folder, wires=physical)
                if not record.get("terminal_known"):
                    raise RuntimeError("Unresolved first Run; stop")
                save(
                    recovered,
                    {
                        **record,
                        "task": key,
                        "provider_receipts": record["physical_receipts"],
                        "physical_receipts": physical,
                    },
                )
                receipt = recovered
            if prior[0] == "active" and receipt.is_file():
                finish(path, key, receipt)
            elif prior != ("complete", file_sha(receipt) if receipt.is_file() else None):
                raise RuntimeError("Unreconciled task or changed record; stop")
            continue
        claim(path, key)
        folder.mkdir(parents=True, exist_ok=False)
        meter = StudyMeter(path, chain)
        meter.task_id = key
        started = time.perf_counter()
        if chain == "citation":
            result = await citation(case, arm, meter, contexts)
        else:
            result = await run_case(case, arm, meter=meter, output=folder)
        with closing(sqlite3.connect(path)) as db:
            physical = wires(db, key)
        save(
            receipt,
            {
                **result,
                "task": key,
                "elapsed_seconds": time.perf_counter() - started,
                "provider_receipts": result.get("physical_receipts", []),
                "physical_receipts": physical,
            },
        )
        finish(path, key, receipt)


def preflight():
    f = verify()
    contexts = load(OUT / "citation-contexts.json")
    for chain, c, arm in cases():
        if chain == "citation":
            request, _ = citation_request(c, arm, contexts)
            if (
                input_bound(
                    {
                        "messages": [
                            {"role": m.role, "content": m.content} for m in request.messages
                        ],
                        "extra_body": {"enable_thinking": False},
                    }
                )
                > INPUT
            ):
                raise ValueError("Frozen citation context exceeds per-request limit")
    print(json.dumps({k: f[k] for k in ("candidate_commit", "max_requests", "cap_microcny")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("freeze").add_argument("commit")
    actions.add_parser("preflight")
    init = actions.add_parser("authorize")
    init.add_argument("directory", type=Path)
    init.add_argument("--approval-id", required=True)
    actions.add_parser("run").add_argument("directory", type=Path)
    args = parser.parse_args()
    if hasattr(args, "directory") and args.directory.resolve() != RUN_DIR.resolve():
        parser.error(f"This authorization uses one fixed run directory: {RUN_DIR}")
    if args.action == "freeze":
        freeze(args.commit)
    elif args.action == "preflight":
        preflight()
    elif args.action == "authorize":
        initialize(args.directory, args.approval_id)
    else:
        from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy

        configure_windows_asyncio_policy()
        asyncio.run(run(args.directory))
