"""Scenario-scoped exact retrieval using AgentHub's production model adapters.

This is not an end-to-end API or tenant-isolation measurement. Source labels
remain solely in the scorer; only a paper/case's unlabelled units are indexed.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from benchmarks.public_rag.scifact_runner import offline_models
from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
)
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.context_selection import select_budgeted_records
from packages.knowledge.contracts import RerankCandidate
from packages.model_gateway.contracts import ModelCapabilities, ModelMessage


def dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def chunks(units, heading: bool, size=900, overlap=150):
    result = []
    for unit in units:
        text = unit["text"]
        prefix = "\n".join(str(unit.get(k, "")) for k in ("title", "section")).strip()
        for start in range(0, len(text), size - overlap):
            end = min(start + size, len(text))
            body = text[start:end]
            result.append(
                {
                    "chunk_id": f"{unit['document_id']}:{start}",
                    "document_id": unit["document_id"],
                    "start": start,
                    "end": end,
                    "text": (prefix + "\n\n" + body) if heading else body,
                    "body": body,
                    "ordinal": unit["ordinal"],
                    "section": unit.get("section", ""),
                }
            )
            if end == len(text):
                break
    return result


def budget_context(query, selected, cap=8000):
    """Admit whole evidence records only and verify the production policy result."""
    from packages.agent_runtime.context_budget import ContextCategory as Cat

    kept = list(
        select_budgeted_records(
            selected,
            max_bytes=cap,
            max_items=max(1, len(selected)),
        )
    )
    messages = (
        ModelMessage("user", query),
        ModelMessage("user", json.dumps(kept, ensure_ascii=False, separators=(",", ":"))),
    )
    policy = ContextBudgetPolicy(
        profiles=(
            SimpleNamespace(
                max_tokens=256, capabilities=ModelCapabilities(max_context_tokens=16384)
            ),
        ),
        config=ContextBudgetConfig(reserved_output_tokens=256, max_retrieval_tokens=cap),
    )
    admitted = policy.admit(messages, categories=(Cat.CURRENT_USER_TASK, Cat.RAG_EVIDENCE))
    if len(admitted.messages) != 2:
        return [], {"status": "BUDGET_FALLBACK", "evidence_bytes": 0}
    try:
        actual = json.loads(admitted.messages[1].content)
    except (ValueError, TypeError):
        return [], {"status": "BUDGET_FALLBACK", "evidence_bytes": 0}
    if actual != kept:
        return [], {"status": "BUDGET_FALLBACK", "evidence_bytes": 0}
    return kept, {
        "status": "ADMITTED",
        "evidence_bytes": len(
            json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ),
    }


def select_context(ranked, all_chunks, units, neighbors=False, fill=False, anchor_limit=5):
    anchors = ranked if fill else ranked[:anchor_limit]
    ordered = list(anchors)
    if neighbors:
        by_id = {u["document_id"]: u for u in units}
        positions = {u["document_id"]: i for i, u in enumerate(units)}
        for anchor in anchors:
            unit = by_id[anchor["document_id"]]
            pos = positions[unit["document_id"]]
            for other in units[max(0, pos - 1) : pos + 2]:
                if other.get("section", "") == unit.get("section", ""):
                    ordered.extend(
                        c for c in all_chunks if c["document_id"] == other["document_id"]
                    )
    seen, output = set(), []
    for c in ordered:
        if c["chunk_id"] not in seen:
            seen.add(c["chunk_id"])
            output.append({k: c[k] for k in ("chunk_id", "document_id", "start", "end", "body")})
    return output


def complete_text_ids(context, units):
    ranges = defaultdict(list)
    for c in context:
        ranges[c["document_id"]].append((c["start"], c["end"]))
    output = []
    for unit in units:
        end, covered = 0, 0
        for left, right in sorted(ranges[unit["document_id"]]):
            covered += max(0, right - max(left, end))
            end = max(end, right)
        if unit["text"] and covered / len(unit["text"]) >= 0.9:
            output.append(unit["document_id"])
    return output


def support_overlap_groups(queries, units):
    """Keep questions sharing exact positive source text in one bootstrap group.

    This uses annotations for offline inference only, never retrieval input.
    """
    texts = {u["document_id"]: " ".join(u["text"].split()) for u in units}
    parents = {q["query_id"]: q["query_id"] for q in queries}

    def root(q):
        while parents[q] != q:
            parents[q] = parents[parents[q]]
            q = parents[q]
        return q

    seen = {}
    for q in queries:
        for source in {source for evidence in q["evidence_sets"] for source in evidence}:
            text = texts.get(source, "")
            if not text:
                continue
            if text in seen:
                left, right = sorted((root(q["query_id"]), root(seen[text])))
                parents[right] = left
            else:
                seen[text] = q["query_id"]
    return {q: root(q) for q in parents}


def load(args):
    if args.dataset == "qasper":
        from benchmarks.public_rag.qasper_data import load_qasper

        return load_qasper(args.data, args.split)
    from benchmarks.public_rag.musique_data import load_musique

    data = load_musique(args.data, args.split, variant="ans")
    return {"corpus": data["documents"], "queries": data["queries"]}


def run(args):
    from benchmarks.public_rag.scenario_metrics import evidence_metrics

    if args.output.exists():
        raise ValueError("Historical run directory exists; use a fresh output")
    args.output.mkdir(parents=True)
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    cfg = protocol[args.dataset]
    expected = (
        cfg["development_split"] if args.phase == "development" else cfg["confirmation_split"]
    )
    if args.split != expected:
        raise ValueError("split does not match declared scenario phase")
    if args.phase == "confirmation" and not args.frozen:
        raise ValueError("Confirmation requires a frozen development selection")
    profiles = cfg["profiles"]
    if args.frozen:
        frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
        if frozen["dataset"] != args.dataset or frozen["protocol"] != protocol:
            raise ValueError("Frozen development method belongs to a different scenario")
        profiles = list(dict.fromkeys(["plain_small", frozen["selected_profile"]]))
    if any(
        p not in {"plain_small", "heading_small", "heading_fill", "heading_neighbor"}
        for p in profiles
    ):
        raise ValueError("API selection requires its explicit second-stage runner")
    dump(
        args.output / "label-access.json",
        {
            "at": datetime.now(UTC).isoformat(),
            "dataset": args.dataset,
            "split": args.split,
            "phase": args.phase,
            "protocol": protocol,
        },
    )
    data = load(args)
    key = "paper_id" if args.dataset == "qasper" else "case_id"
    all_groups = sorted({q[key] for q in data["queries"]})
    excluded = cfg.get("exclude_group_ids", [])
    if not isinstance(excluded, list) or any(not isinstance(v, str) for v in excluded):
        raise ValueError("exclude_group_ids must be a list of source-group IDs")
    if len(set(excluded)) != len(excluded):
        raise ValueError("duplicate excluded source groups")
    all_groups = [group for group in all_groups if group not in set(excluded)]
    random.Random(protocol["seed"]).shuffle(all_groups)
    field = "development_papers" if args.phase == "development" else "confirmation_papers"
    if args.dataset == "musique":
        field = "development_cases" if args.phase == "development" else "confirmation_cases"
    limit = cfg[field]
    if type(limit) is int:
        all_groups = all_groups[:limit]
    groups = set(all_groups)
    queries = [q for q in data["queries"] if q[key] in groups]
    inference_groups = (
        {q["query_id"]: q[key] for q in queries}
        if args.dataset == "qasper"
        else support_overlap_groups(queries, [u for u in data["corpus"] if u[key] in groups])
    )
    unit_groups, query_groups = defaultdict(list), defaultdict(list)
    for unit in data["corpus"]:
        if unit[key] in groups:
            unit_groups[unit[key]].append(unit)
    for q in queries:
        query_groups[q[key]].append(q)
    dump(
        args.output / "meta.json",
        {
            "dataset": args.dataset,
            "split": args.split,
            "phase": args.phase,
            "groups": len(groups),
            "queries": len(queries),
            "resampling_groups": len(set(inference_groups.values())),
            "profiles": profiles,
            "protocol": protocol,
            "group_ids": all_groups,
            "model": protocol["models"],
            "scope": cfg["task"],
            "timing": "shared cached profile inference; not paired E2E",
        },
    )
    offline_models()
    dense = BgeM3DenseEmbedder(device="cuda", batch_size=8)
    reranker = BgeReranker(device="cuda", batch_size=8)
    dense.warm()
    reranker.warm()
    completed = 0
    with (args.output / "run.jsonl").open("x", encoding="utf-8") as out:
        for group in all_groups:
            units = unit_groups[group]
            group_queries = query_groups[group]
            query_vectors = np.asarray(dense.embed_documents([q["query"] for q in group_queries]))
            prepared = {}
            for heading in {p != "plain_small" for p in profiles}:
                cs = chunks(units, heading, cfg["chunk_chars"], cfg["overlap_chars"])
                if cs:
                    vs = np.asarray(dense.embed_documents([c["text"] for c in cs]))
                    similarity = query_vectors @ vs.T
                else:
                    similarity = np.zeros((len(group_queries), 0))
                prepared[heading] = cs, similarity
            for i, query in enumerate(group_queries):
                started = time.perf_counter()
                ranked_cache, arms = {}, {}
                for profile in profiles:
                    heading = profile != "plain_small"
                    cs, similarity = prepared[heading]
                    if heading not in ranked_cache:
                        order = sorted(range(len(cs)), key=lambda j: (-float(similarity[i, j]), j))[
                            : cfg["dense_candidates"]
                        ][: cfg["rerank_candidates"]]
                        candidates = [cs[j] for j in order]
                        scores = reranker.rerank(
                            query["query"],
                            [
                                RerankCandidate(chunk_id=c["chunk_id"], text=c["text"])
                                for c in candidates
                            ],
                        )
                        ranked_cache[heading] = [
                            {**c, "rerank_score": -neg_score}
                            for neg_score, _, c in sorted(
                                (
                                    (-score, j, c)
                                    for j, (c, score) in enumerate(
                                        zip(candidates, scores, strict=True)
                                    )
                                ),
                                key=lambda v: (v[0], v[1]),
                            )
                        ]
                    ranked = ranked_cache[heading]
                    context, budget = budget_context(
                        query["query"],
                        select_context(
                            ranked,
                            cs,
                            units,
                            neighbors=profile == "heading_neighbor",
                            fill=profile == "heading_fill",
                            anchor_limit=cfg["retrieval_anchor_chunks"],
                        ),
                        cap=cfg["context_byte_budget"],
                    )
                    selected = list(dict.fromkeys(c["document_id"] for c in context))
                    metric = evidence_metrics(query["evidence_sets"], selected)
                    full = evidence_metrics(
                        query["evidence_sets"], complete_text_ids(context, units)
                    )
                    metric.update(
                        full_text_evidence_recall=full["best_evidence_recall"],
                        selected_paragraphs=len(selected),
                        selected_ids=selected,
                        complete_text_ids=complete_text_ids(context, units),
                        rerank_ranking=[
                            {k: c[k] for k in ("chunk_id", "document_id", "rerank_score")}
                            for c in ranked
                        ],
                        **budget,
                    )
                    arms[profile] = metric
                row = {
                    "query_id": query["query_id"],
                    "group_id": inference_groups[query["query_id"]],
                    "answerable": query["answerable"],
                    "hops": query.get("hops"),
                    "unaligned_evidence_count": len(query.get("unaligned_evidence", [])),
                    "evidence_alignment_complete": query.get("evidence_alignment_complete", True),
                    "arms": arms,
                    "inference_ms": (time.perf_counter() - started) * 1000,
                }
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                completed += 1
            print(
                f"group {len(all_groups)} completed queries {completed}/{len(queries)}", flush=True
            )
    dump(args.output / "completed.json", {"queries": completed, "expected": len(queries)})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", choices=("qasper", "musique"), required=True)
    p.add_argument("--split", required=True)
    p.add_argument("--phase", choices=("development", "confirmation"), required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--frozen", type=Path)
    run(p.parse_args())


if __name__ == "__main__":
    main()
