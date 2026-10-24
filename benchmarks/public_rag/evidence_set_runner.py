"""Fixed complementary-set experiment over cached DEV ranks and real consumer admission."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx

from benchmarks.public_rag.consumer_answer_client import (
    EXPERIMENT_MODEL,
    _observed_usage,
    _strict_json_loads,
)
from benchmarks.public_rag.consumer_replay import (
    CONTEXT,
    KB,
    SNAPSHOT,
    CachedConsumerRetriever,
    _Session,
    _spec,
    compact,
    definition,
    recover_records,
    score_records,
)
from benchmarks.public_rag.evidence_plan_replay import admit
from benchmarks.public_rag.fiqa_confirmation import _credentials
from benchmarks.public_rag.scenario_metrics import paired_group_ci
from benchmarks.public_rag.scenario_runner import chunks, dump, load
from packages.knowledge.contracts import RetrievalQuery
from packages.knowledge.evidence_records import evidence_record, evidence_reference
from packages.knowledge.evidence_set import EvidenceSetSelector
from packages.knowledge.evidence_set_retriever import EvidenceSetKnowledgeRetriever
from packages.model_gateway.contracts import ModelCapabilities, ModelResponse
from packages.tools.builtins.search_knowledge import search_knowledge
from packages.tools.contracts import ToolExecutionContext

PROFILES = ["legacy5", "set_rank5", "set_read5"]


def structure_gate(comparisons, *, complete, valid_count, planned, ordering_equal, protocol):
    cfg = protocol["stage1"]
    return (
        complete
        and planned > 0
        and all(c["inference_valid"] for c in comparisons.values())
        and comparisons["complete"]["delta"] >= cfg["min_gain"]
        and comparisons["complete"]["ci_low"] > cfg["paired_group95_ci_low_above"]
        and comparisons["recall"]["delta"] >= -cfg["max_mean_support_recall_loss"]
        and valid_count / planned >= cfg["min_selector_valid_fraction"]
        and ordering_equal
    )


class SelectionFailure(Exception):
    pass


class CapturedSelection:
    """Reuse the one observed selector decision, never issue a second model call."""

    def __init__(self, candidates, plan):
        self.candidates, self.plan = candidates, plan

    async def select(self, context, question, candidates, *, max_items):
        if context != CONTEXT or candidates != self.candidates or max_items != 5:
            raise ValueError("composed selector candidate snapshot differs")
        return self.plan


class SelectionGateway:
    """Benchmark-only HTTP adapter, same fixed model and single-attempt policy."""

    def __init__(self, client, endpoint, key, events, query_id):
        self.client, self.endpoint, self.key = client, endpoint, key
        self.events, self.query_id = events, query_id
        self.result = {"status": "NOT_ATTEMPTED", "usage": None, "request_ms": 0.0}

    def log(self, kind, **fields):
        self.events.write(compact({"event": kind, "query_id": self.query_id, **fields}) + "\n")
        self.events.flush()

    async def generate_resolved(self, context, plan, request):
        if context != CONTEXT or plan.primary.model != EXPERIMENT_MODEL:
            raise ValueError("benchmark binding mismatch")
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        self.log("dispatch", messages=messages)
        self.result["status"] = "UNKNOWN_OUTCOME"
        start = time.perf_counter()
        try:
            response = await self.client.post(
                self.endpoint,
                headers={"Authorization": "Bearer " + self.key},
                json={
                    "model": EXPERIMENT_MODEL,
                    "temperature": 0,
                    "max_tokens": 256,
                    "thinking": {"type": "disabled"},
                    "response_format": {"type": "json_object"},
                    "messages": messages,
                },
                follow_redirects=False,
            )
        except httpx.RequestError:
            self.log("outcome", api=self.result)
            raise SelectionFailure from None
        finally:
            self.result["request_ms"] = (time.perf_counter() - start) * 1000
        if not response.is_success:
            self.result.update(status="PROVIDER_ERROR", http_status=response.status_code)
            try:
                error = response.json().get("error", {})
                if isinstance(error, dict):
                    self.result["provider_error"] = {
                        k: str(error[k]).replace(self.key, "[REDACTED]")[:240]
                        for k in ("type", "code", "message")
                        if k in error
                    }
            except (ValueError, AttributeError):
                pass
            self.log("outcome", api=self.result)
            raise SelectionFailure
        try:
            body = _strict_json_loads(response.content)
            self.result["usage"] = _observed_usage(body.get("usage"))
            if body.get("model") != EXPERIMENT_MODEL:
                self.result["status"] = "MODEL_IDENTITY_ERROR"
                raise SelectionFailure
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("missing content")
            self.result.update(status="RECEIVED", content=content)
            return ModelResponse(content, "openai-compatible", EXPERIMENT_MODEL)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            self.result["status"] = "INVALID_OUTPUT"
            raise SelectionFailure from None
        finally:
            self.log("outcome", api=self.result)


def payload_for(evidence):
    return {
        "results": [evidence_record(e) for e in evidence],
        "evidence_refs": [
            evidence_reference(e, snapshot_id=str(SNAPSHOT), knowledge_base_id=str(KB))
            for e in evidence
        ],
    }


async def capture(question, payload, tool, mapping, units, query, candidate_metric):
    observed, final, messages, budget, _, _ = await admit(question, payload, tool)
    final_records = recover_records(final)
    return {
        "messages": messages,
        "budget": budget,
        "admission_verified": True,
        "admitted_chunk_ids": [r["chunk_id"] for r in final_records],
        "source_chunk_ids": [r["chunk_id"] for r in final_records],
        "final_evidence_bytes": len(compact(final).encode("utf-8")),
        "stages": {
            "cached_candidate_pool": candidate_metric,
            "tool_return": score_records(payload["results"], mapping, units, query),
            "runtime_observation": score_records(recover_records(observed), mapping, units, query),
            "final_admitted_context": score_records(final_records, mapping, units, query),
        },
        "final_records": final_records,
    }


async def run(args):
    if args.output.exists():
        raise ValueError("refusing selection replay or overwrite")
    meta = json.loads((args.baseline / "meta.json").read_text(encoding="utf-8"))
    done = json.loads((args.baseline / "completed.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(s)
        for s in (args.baseline / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if not len(rows) == meta["queries"] == done["expected"]:
        raise ValueError("full frozen candidate run required")
    if len({r["query_id"] for r in rows}) != len(rows) or meta["dataset"] != "musique":
        raise ValueError("unique MuSiQue questions required")
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    prior = {}
    if args.continue_from is not None:
        prior_meta = json.loads((args.continue_from / "meta.json").read_text(encoding="utf-8"))
        if prior_meta["protocol"] != protocol or prior_meta["baseline"] != str(args.baseline):
            raise ValueError("continuation must preserve protocol and candidate run")
        prior = {
            r["query_id"]: r
            for r in map(
                json.loads,
                (args.continue_from / "prepared.jsonl").read_text(encoding="utf-8").splitlines(),
            )
        }
        if not set(prior).issubset({r["query_id"] for r in rows}):
            raise ValueError("unexpected prior questions")
        for row in prior.values():
            api = row["selection"]
            if api["status"] in {"UNKNOWN_OUTCOME", "MODEL_IDENTITY_ERROR"} or (
                api["status"] == "PROVIDER_ERROR" and api.get("http_status") != 400
            ):
                raise ValueError("ambiguous prior request cannot be continued")
    args.dataset, args.split = meta["dataset"], meta["split"]
    data = load(args)
    queries = {q["query_id"]: q for q in data["queries"]}
    wanted = {queries[r["query_id"]]["case_id"] for r in rows}
    units_by_case = defaultdict(list)
    for unit in data["corpus"]:
        if unit["case_id"] in wanted:
            units_by_case[unit["case_id"]].append(unit)
    input_snapshot = [
        {
            "candidate_row": r,
            "question": queries[r["query_id"]]["query"],
            "source_units": units_by_case[queries[r["query_id"]]["case_id"]],
        }
        for r in rows
    ]
    if args.continue_from is not None:
        prior_input = json.loads(
            (args.continue_from / "input-snapshot.json").read_text(encoding="utf-8")
        )
        if prior_input != input_snapshot:
            raise ValueError("continuation source text, question or candidate ranks changed")
    args.output.mkdir(parents=True)
    dump(args.output / "input-snapshot.json", input_snapshot)
    dump(
        args.output / "meta.json",
        {
            "baseline": str(args.baseline),
            "dataset": args.dataset,
            "split": args.split,
            "protocol": protocol,
            "queries": len(rows),
            "profiles": PROFILES,
            "scope": __doc__,
            "label_policy": "question/authorized text only in selector; gold only scorer",
            "prior_run": str(args.continue_from) if args.continue_from else None,
        },
    )
    dump(args.output / "label-access.json", {"scope": "already exposed DEV, gold scoring only"})
    endpoint, key = _credentials()
    stop = asyncio.Event()
    results = dict(prior)
    selection_results = {k: v["selection"] for k, v in prior.items()}
    plan = _spec({}).model_plan
    plan = replace(
        plan,
        primary=replace(plan.primary, capabilities=ModelCapabilities(max_context_tokens=32768)),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with httpx.AsyncClient(
        timeout=30, transport=httpx.AsyncHTTPTransport(retries=0)
    ) as client:
        with (args.output / "selection-events.jsonl").open("x", encoding="utf-8") as events:
            iterator = iter((i, r) for i, r in enumerate(rows, 1) if r["query_id"] not in prior)

            async def one(case_index, row):
                query = queries[row["query_id"]]
                units = units_by_case[query["case_id"]]
                cfg = meta["protocol"]["musique"]
                raw = chunks(units, True, cfg["chunk_chars"], cfg["overlap_chars"])
                native = {c["chunk_id"]: c for c in raw}
                ids = {
                    c["chunk_id"]: str(UUID(int=case_index * 1000000 + i + 1))
                    for i, c in enumerate(raw)
                }
                mapping = {ids[c["chunk_id"]]: {**c, "chunk_id": ids[c["chunk_id"]]} for c in raw}
                ranked = [
                    {
                        **native[c["chunk_id"]],
                        "chunk_id": ids[c["chunk_id"]],
                        "rerank_score": c["rerank_score"],
                    }
                    for c in row["arms"]["heading_fill"]["rerank_ranking"]
                ]
                retriever = CachedConsumerRetriever(ranked, units, id_offset=case_index * 10000)
                tool = definition()
                baseline = await search_knowledge(
                    ToolExecutionContext(CONTEXT, UUID(int=9), "consumer-retrieval"),
                    tool,
                    {"query": query["query"], "limit": 5},
                    _Session,
                    retriever=retriever,
                )
                pool = await retriever.retrieve_with_trace(
                    CONTEXT,
                    RetrievalQuery(
                        text=query["query"],
                        knowledge_base_id=str(KB),
                        knowledge_snapshot_id=str(SNAPSHOT),
                        dense_top_k=20,
                        sparse_top_k=20,
                        candidate_top_k=20,
                        final_top_k=20,
                    ),
                )
                candidates = pool.evidence
                candidate_metric = score_records(
                    [{"chunk_id": e.chunk_id, "snippet": e.text[:1000]} for e in candidates],
                    mapping,
                    units,
                    query,
                )
                gateway = SelectionGateway(client, endpoint, key, events, row["query_id"])
                started = time.perf_counter()
                try:
                    selected = await EvidenceSetSelector(gateway, plan).select(
                        CONTEXT, query["query"], candidates, max_items=5
                    )
                    gateway.result["status"] = "VALID"
                    composed = EvidenceSetKnowledgeRetriever(
                        retriever, CapturedSelection(candidates, selected)
                    )
                    rank_payload = await search_knowledge(
                        ToolExecutionContext(CONTEXT, UUID(int=9), "consumer-retrieval"),
                        tool,
                        {"query": query["query"], "limit": 5},
                        _Session,
                        retriever=composed,
                    )
                    by_id = {
                        e.chunk_id: e
                        for e in selected.rank_order
                        if e.chunk_id in {r["chunk_id"] for r in rank_payload["results"]}
                    }
                    read_evidence = tuple(by_id[i] for i in selected.reading_order if i in by_id)
                    candidate_payloads = (rank_payload, payload_for(read_evidence))
                    gateway.result["selected_ids"] = list(selected.reading_order)
                except (SelectionFailure, ValueError):
                    if gateway.result["status"] in {"RECEIVED", "NOT_ATTEMPTED"}:
                        gateway.result["status"] = "INVALID_OUTPUT"
                    candidate_payloads = (baseline, baseline)
                elapsed = (time.perf_counter() - started) * 1000
                gateway.log("validated", api=gateway.result)
                selection_results[row["query_id"]] = {**gateway.result, "pipeline_ms": elapsed}
                status = gateway.result["status"]
                if status in {"UNKNOWN_OUTCOME", "MODEL_IDENTITY_ERROR"} or (
                    status == "PROVIDER_ERROR" and gateway.result.get("http_status") != 400
                ):
                    stop.set()
                if sum(s["status"] == "INVALID_OUTPUT" for s in selection_results.values()) > max(
                    3, int(len(rows) * 0.05)
                ):
                    stop.set()
                arms = {}
                for profile, payload in zip(
                    PROFILES[:2], (baseline, candidate_payloads[0]), strict=True
                ):
                    arms[profile] = await capture(
                        query["query"], payload, tool, mapping, units, query, candidate_metric
                    )
                # Reorder only rank-arm ACTUALLY ADMITTED fragments, not a wider pre-budget set.
                rank_records = arms["set_rank5"]["final_records"]
                reference_by_id = {r["chunk_id"]: r for r in candidate_payloads[0]["evidence_refs"]}
                record_by_id = {r["chunk_id"]: r for r in rank_records}
                order = (
                    [e["chunk_id"] for e in candidate_payloads[1]["results"]]
                    if gateway.result["status"] == "VALID"
                    else list(record_by_id)
                )
                order = [i for i in order if i in record_by_id]
                read_payload = {
                    "results": [record_by_id[i] for i in order],
                    "evidence_refs": [reference_by_id[i] for i in order],
                }
                arms["set_read5"] = await capture(
                    query["query"], read_payload, tool, mapping, units, query, candidate_metric
                )
                # The ordering diagnostic is eligible only if the actual visible fragments match.
                rank_frag = {compact(r) for r in arms["set_rank5"]["final_records"]}
                read_frag = {compact(r) for r in arms["set_read5"]["final_records"]}
                results[row["query_id"]] = {
                    "query_id": row["query_id"],
                    "group_id": row["group_id"],
                    "hops": row.get("hops"),
                    "answers": query["answers"],
                    "answerable": query["answerable"],
                    "arms": arms,
                    "selection": selection_results[row["query_id"]],
                    "ordering_fragments_equal": rank_frag == read_frag,
                }

            async def worker():
                while not stop.is_set():
                    job = next(iterator, None)
                    if job is None:
                        return
                    await one(*job)

            first = next(iterator, None)
            if first is None:
                raise ValueError("no unattempted questions remain")
            await one(*first)
            first_api = selection_results[first[1]["query_id"]]
            if first_api["status"] != "VALID":
                stop.set()
            await asyncio.gather(*(worker() for _ in range(protocol["selection_workers"])))
    output_rows = [results[r["query_id"]] for r in rows if r["query_id"] in results]
    with (args.output / "prepared.jsonl").open("x", encoding="utf-8") as out:
        for row in output_rows:
            out.write(compact(row) + "\n")
    complete = (
        len(output_rows) == len(rows)
        and not stop.is_set()
        and all(
            r["status"] not in {"UNKNOWN_OUTCOME", "MODEL_IDENTITY_ERROR"}
            for r in selection_results.values()
        )
    )
    dump(
        args.output / "completed.json",
        {
            "expected": len(rows),
            "queries": len(output_rows),
            "run_state": "COMPLETE" if complete else "NEEDS_ATTENTION",
            "selection_calls": sum(
                r["status"] != "NOT_ATTEMPTED" for r in selection_results.values()
            ),
        },
    )
    structural = [
        {
            "query_id": r["query_id"],
            "group_id": r["group_id"],
            "arms": {
                p: {
                    "complete": r["arms"][p]["stages"]["final_admitted_context"][
                        "complete_evidence_set"
                    ],
                    "recall": r["arms"][p]["stages"]["final_admitted_context"][
                        "best_evidence_recall"
                    ],
                }
                for p in PROFILES
            },
        }
        for r in output_rows
    ]
    comparisons = {
        metric: paired_group_ci(
            structural,
            metric,
            "legacy5",
            "set_rank5",
            group_key="group_id",
            seed=protocol["seed"],
            samples=5000,
            confidence=0.95,
        )
        for metric in ("complete", "recall")
    }
    statuses = {
        s: sum(r["status"] == s for r in selection_results.values())
        for s in {r["status"] for r in selection_results.values()}
    }
    gate = structure_gate(
        comparisons,
        complete=complete,
        planned=len(rows),
        valid_count=statuses.get("VALID", 0),
        protocol=protocol,
        ordering_equal=all(r["ordering_fragments_equal"] for r in output_rows),
    )
    dump(
        args.output / "structure.json",
        {
            "comparisons": comparisons,
            "statuses": statuses,
            "answer_stage_allowed": gate,
            "ordering_equal": sum(r["ordering_fragments_equal"] for r in output_rows),
            "queries": len(output_rows),
            "complete": complete,
        },
    )
    print(
        compact(
            {
                "questions": len(output_rows),
                "selection_statuses": statuses,
                "answer_stage_allowed": gate,
                "comparisons": comparisons,
            }
        )
    )
    if not complete:
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "data", "protocol", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--continue-from", type=Path)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
