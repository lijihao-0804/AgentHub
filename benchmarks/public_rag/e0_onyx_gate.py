"""P-E0 regression gate: the Onyx opt-in must not degrade the frozen funnel.

Two zero-API passes over the frozen 200-question capture:
  1. flag installed, selector unavailable  -> fail-closed identity fallback
  2. flag installed, identity selector      -> plumbing adds no loss
An optional live smoke (--live N) exercises the real gateway selector on at
most N questions. Artifacts land in results/e0-onyx-20261024/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from benchmarks.public_rag.consumer_replay import (
    CONTEXT,
    CachedConsumerRetriever,
    _Session,
    admit_result,
    definition,
    recover_records,
    score_records,
)
from benchmarks.public_rag.scenario_runner import chunks, dump, load
from packages.knowledge.adapters.onyx_selection import OpenAICompatibleSectionSelector
from packages.tools.builtins.search_knowledge import search_knowledge
from packages.tools.contracts import ToolExecutionContext

RESULTS = Path(__file__).parent / "results" / "e0-onyx-20261024"
BASELINE = Path(__file__).parent / "results" / "consumer-rag-20261023" / "confirmation-candidates-1"
DATA = Path("data/external/rag-public-20261021/musique/extracted/data")
PROTOCOL = Path(__file__).parent / "results" / "consumer-rag-20261023" / "protocol.json"


class _IdentitySelector:
    async def select_sections(self, context, query, sections):
        return ()


def _onyx_profile(base):
    return replace(
        base,
        retrieval_config={
            **base.retrieval_config,
            "final_top_k": 5,  # onyx profile ceiling is 10; keep both arms equal
            "evidence_set_selector": "onyx_v1",
        },
    )


def _baseline_profile(base):
    return replace(base, retrieval_config={**base.retrieval_config, "final_top_k": 5})


async def _tool_payload(retriever, tool_definition, query_text, selector):
    return await search_knowledge(
        ToolExecutionContext(CONTEXT, UUID(int=9), "e0-gate"),
        tool_definition,
        {"query": query_text, "limit": 5},
        _Session,
        retriever=retriever,
        section_selector=selector,
    )


async def gate(args) -> None:
    meta = json.loads((BASELINE / "meta.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (BASELINE / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    args.dataset, args.split = meta["dataset"], meta["split"]
    args.data = DATA
    data = load(args)
    queries = {q["query_id"]: q for q in data["queries"]}
    key = "case_id"
    unit_groups: dict[str, list] = {}
    wanted = {queries[r["query_id"]][key] for r in rows}
    for unit in data["corpus"]:
        if unit[key] in wanted:
            unit_groups.setdefault(unit[key], []).append(unit)

    base = definition()
    profiles = {"flag_off": _baseline_profile(base), "flag_on": _onyx_profile(base)}
    selectors = {"flag_on_unconfigured": None, "flag_on_identity": _IdentitySelector()}

    per_question: list[dict] = []
    mismatches = 0
    for case_index, row in enumerate(rows, start=1):
        query = queries[row["query_id"]]
        units = unit_groups[query[key]]
        cfg = meta["protocol"][args.dataset]
        cs = chunks(units, True, cfg["chunk_chars"], cfg["overlap_chars"])
        native_mapping = {c["chunk_id"]: c for c in cs}
        opaque_ids = {
            c["chunk_id"]: str(UUID(int=case_index * 1_000_000 + i + 1))
            for i, c in enumerate(cs)
        }
        mapping = {
            opaque_ids[c["chunk_id"]]: {**c, "chunk_id": opaque_ids[c["chunk_id"]]} for c in cs
        }
        ranked = [
            {
                **native_mapping[c["chunk_id"]],
                "chunk_id": opaque_ids[c["chunk_id"]],
                "rerank_score": c["rerank_score"],
            }
            for c in row["arms"]["heading_fill"]["rerank_ranking"]
        ]
        retriever = CachedConsumerRetriever(ranked, units, id_offset=case_index * 10_000)

        record: dict = {"query_id": row["query_id"]}
        reference_ids = None
        reference_final = None
        for profile_name, tool_definition in profiles.items():
            selector = selectors.get(profile_name) if profile_name == "flag_on" else None
            payload = await _tool_payload(retriever, tool_definition, query["query"], selector)
            ids = [r["chunk_id"] for r in recover_records(payload)]
            _, final, _, _ = await admit_result(query["query"], payload, tool_definition)
            final_ids = [r["chunk_id"] for r in recover_records(final)]
            if profile_name == "flag_off":
                reference_ids, reference_final = ids, final_ids
                record["baseline"] = {
                    "tool_ids": ids,
                    "final_ids": final_ids,
                    "tool_recall": score_records(
                        recover_records(payload), mapping, units, query
                    )["best_evidence_recall"],
                }
            else:
                record["flag_on_unconfigured"] = {
                    "tool_equal": ids == reference_ids,
                    "final_equal": final_ids == reference_final,
                }

        # identity-selector arm re-runs the same wrapper with a real selector
        for arm_name, selector in selectors.items():
            if selector is None:
                continue
            payload = await _tool_payload(retriever, profiles["flag_on"], query["query"], selector)
            ids = [r["chunk_id"] for r in recover_records(payload)]
            _, final, _, _ = await admit_result(query["query"], payload, profiles["flag_on"])
            final_ids = [r["chunk_id"] for r in recover_records(final)]
            record[arm_name] = {
                "tool_equal": ids == reference_ids,
                "final_equal": final_ids == reference_final,
            }

        if not (
            record["flag_on_unconfigured"]["tool_equal"]
            and record["flag_on_unconfigured"]["final_equal"]
            and record["flag_on_identity"]["tool_equal"]
            and record["flag_on_identity"]["final_equal"]
        ):
            mismatches += 1
        per_question.append(record)

    summary = {
        "questions": len(per_question),
        "mismatches": mismatches,
        "gate": "PASS" if mismatches == 0 else "FAIL",
        "scope": "flag on/off + selector unavailable/identity must be byte-identical",
    }
    dump(RESULTS / "gate.json", summary)
    dump(RESULTS / "gate-questions.json", per_question)
    print(f"gate: {summary['gate']} ({mismatches} mismatches over {len(per_question)})")


async def live_smoke(args) -> None:
    base_url = os.environ.get("AGENTHUB_RAG_BASE_URL", "").strip("\"'")
    api_key = os.environ.get("AGENTHUB_RAG_API_KEY", "").strip("\"'")
    if not base_url or not api_key:
        for line in Path(".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("AGENTHUB_RAG_BASE_URL="):
                base_url = line.split("=", 1)[1].strip().strip("\"'")
            elif line.startswith("AGENTHUB_RAG_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip("\"'")
    live = OpenAICompatibleSectionSelector(base_url=base_url, api_key=api_key,
                                           model="deepseek-v4.1-flash-1017")

    class _SpySelector:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def select_sections(self, context, query, sections):
            ids = await live.select_sections(context, query, sections)
            self.calls.append({"query": query, "sections": len(sections), "ids": list(ids)})
            return ids

    selector = _SpySelector()

    meta = json.loads((BASELINE / "meta.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (BASELINE / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ][: args.live]
    args.dataset, args.split = meta["dataset"], meta["split"]
    args.data = DATA
    data = load(args)
    queries = {q["query_id"]: q for q in data["queries"]}
    key = "case_id"
    base = definition()
    tool_definition = _onyx_profile(base)

    records = []
    for case_index, row in enumerate(rows, start=1):
        query = queries[row["query_id"]]
        units = [u for u in data["corpus"] if u[key] == query[key]]
        cfg = meta["protocol"][args.dataset]
        cs = chunks(units, True, cfg["chunk_chars"], cfg["overlap_chars"])
        native_mapping = {c["chunk_id"]: c for c in cs}
        opaque_ids = {
            c["chunk_id"]: str(UUID(int=case_index * 1_000_000 + i + 1))
            for i, c in enumerate(cs)
        }
        ranked = [
            {
                **native_mapping[c["chunk_id"]],
                "chunk_id": opaque_ids[c["chunk_id"]],
                "rerank_score": c["rerank_score"],
            }
            for c in row["arms"]["heading_fill"]["rerank_ranking"]
        ]
        retriever = CachedConsumerRetriever(ranked, units, id_offset=case_index * 10_000)
        baseline_payload = await _tool_payload(
            retriever, _baseline_profile(base), query["query"], None
        )
        baseline_ids = [r["chunk_id"] for r in baseline_payload["results"]]
        payload = await _tool_payload(retriever, tool_definition, query["query"], selector)
        call = selector.calls[-1] if selector.calls else None
        records.append(
            {
                "query_id": row["query_id"],
                "sections": call["sections"] if call else None,
                "selected_ids": call["ids"] if call else None,
                "delivered": len(payload["results"]),
                "membership_changed": sorted(r["chunk_id"] for r in payload["results"])
                != sorted(baseline_ids),
            }
        )
    dump(
        RESULTS / "live-smoke.json",
        {
            "requests": len(records),
            "model": "deepseek-v4.1-flash-1017",
            "records": records,
            "selector_calls": len(selector.calls),
            "selected": sum(1 for r in records if r["selected_ids"] is not None),
            "membership_changed": sum(1 for r in records if r["membership_changed"]),
        },
    )
    print(
        f"live smoke: {len(selector.calls)} selector calls, "
        f"{sum(1 for r in records if r['selected_ids'] is not None)} valid selections, "
        f"{sum(1 for r in records if r['membership_changed'])} membership changes"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", type=int, default=0, help="live smoke questions (<= 10)")
    args = parser.parse_args()
    if args.live:
        asyncio.run(live_smoke(args))
    else:
        asyncio.run(gate(args))


if __name__ == "__main__":
    main()
