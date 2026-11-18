"""Label-free six-slot merge/refill with a per-query, per-budget source-retention guard."""

import argparse
import asyncio
import hashlib
import inspect
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from benchmarks.public_rag.experiments.sixth_source_retention.inputs import Window, fiqa, scifact
from benchmarks.public_rag.experiments.sixth_source_retention.replay import covers, outcomes
from packages.agent_runtime.context_budget import ApproxTokenEstimator
from packages.knowledge.citation_qa import _evidence_prompt, _select_evidence
from packages.knowledge.contracts import RetrievedEvidence
from packages.knowledge.evidence_records import MAX_EVIDENCE_EXCERPT_CHARS

HERE = Path(__file__).resolve().parent
PROTOCOL = "cb9f69f0b46148988c6c8c1d2457eccfef4e6cf79de82d2e1bbffd92a33c341c"
PREVIOUS = HERE.parent / "sixth_source_retention/results/summary.json"
BUDGETS = (512, 16000)
EST = ApproxTokenEstimator()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def merge(left, right, source):
    if not left.revision or (left.doc, left.revision) != (right.doc, right.revision):
        return None
    lo, hi = min(left.start, right.start), max(left.end, right.end)
    if max(left.start, right.start) > min(left.end, right.end):
        return None
    parts = []
    for w in (left, right):
        if not (
            w.locator and 0 <= w.start < w.end <= len(source) and source[w.start : w.end] == w.text
        ):
            return None
        parts.append(
            {"chunk_id": w.chunk, "locator": w.locator, "displayed_span": [w.start, w.end]}
        )
    if not covers(lo, hi, [p["displayed_span"] for p in parts]):
        raise ValueError("Composite introduces an invisible character")
    locator = {
        "type": "research_source_union",
        "char_start": lo,
        "char_end": hi,
        "components": parts,
    }
    digest = sha(encoded([left.doc, left.revision, locator]))
    return Window(
        "research-union-" + digest, left.doc, left.revision, lo, hi, source[lo:hi], locator
    )


@dataclass(frozen=True)
class Proposal:
    top: list[Window]
    left_rank: int
    right_rank: int
    refill_rank: int


def propose(ranked, texts):
    original = ranked[:6]
    if len(original) < 6:
        return None
    sources = {w.doc for w in original}
    refill = next(((k, w) for k, w in enumerate(ranked[6:20], 7) if w.doc not in sources), None)
    if refill is None:
        return None
    for i, j in combinations(range(6), 2):
        joined = merge(original[i], original[j], texts[original[i].doc])
        if joined:
            top = [joined if k == i else w for k, w in enumerate(original) if k != j]
            return Proposal([*top, refill[1]], i + 1, j + 1, refill[0])
    return None


def retains(baseline, candidate):
    known = defaultdict(list)
    for w in candidate:
        if w.revision:
            known[(w.doc, w.revision)].append((w.start, w.end))
    return all(
        covers(w.start, w.end, known[(w.doc, w.revision)])
        if w.revision
        else any(w == other for other in candidate)
        for w in baseline
    )


@dataclass(frozen=True)
class Message:
    windows: list[Window]
    text: str
    body_units: int


def pack(query, top, budget, corpus):
    records = tuple(
        RetrievedEvidence(
            w.doc,
            w.revision or "",
            w.chunk,
            f"{corpus}:{w.doc}",
            w.locator,
            w.text,
            0.0,
            None,
            {},
        )
        for w in top
    )
    items = _select_evidence(records, max_chars=budget)
    prompt = _evidence_prompt(query, items)
    by_id = {w.chunk: w for w in top}
    windows = [by_id[item.evidence.chunk_id] for item in items]
    for i, (item, w) in enumerate(zip(items, windows, strict=True), 1):
        loc = encoded(w.locator).decode()
        section = f"\n[{i}]\nsource: {corpus}:{w.doc}\nlocator: {loc}\ntext:\n{w.text}"
        if item.text != w.text or section not in prompt:
            raise ValueError("Serialized message differs from its certified source/locator/text")
    cost = sum(EST.estimate(w.text) for w in windows)
    if len(windows) > 6 or cost > budget or (budget == 16000 and windows != top):
        raise ValueError("Passage count or budget invariants failed")
    return Message(windows, prompt, cost)


def record_coverage(counts, value, gold):
    counts["macro_source_recall"] += len(value["source"]) / len(gold)
    for metric, matched in value.items():
        counts[metric + "_pairs"] += len(matched)
        counts[metric + "_hit_queries"] += bool(matched)


def delta(before, after, total):
    changed = {}
    for metric, old in before.items():
        new = after[metric]
        gained, lost = new - old, old - new
        total[metric + "_gained"] += len(gained)
        total[metric + "_lost"] += len(lost)
        total[metric + "_hit_queries_gained"] += bool(new) and not old
        total[metric + "_hit_queries_lost"] += bool(old) and not new
        if gained or lost:
            changed[metric] = {"gained": sorted(gained), "lost": sorted(lost)}
    return changed


def evaluate(name, inputs, reference):
    src, ranks, texts, official, provenance = inputs
    totals, paired, admission = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    projections = {(a, b): hashlib.sha256() for a in ("A", "B") for b in BUDGETS}
    cite_digests = {(a, b): hashlib.sha256() for a in ("A", "B") for b in BUDGETS}
    changes = []
    examples = {}
    for q, ranking in ranks.items():
        gold = {d for d, grade in src.qrels[q].items() if grade > 0}
        baseline = ranking[:6]
        proposal = propose(ranking, texts)
        for budget in BUDGETS:
            a = pack(src.queries[q], baseline, budget, name)
            b = a
            route = admission[budget]
            if proposal:
                route["eligible_proposals"] += 1
                attempt = pack(src.queries[q], proposal.top, budget, name)
                if retains(a.windows, attempt.windows):
                    route["accepted"] += 1
                    b = attempt
                    route["visible_refill"] += proposal.top[-1] in b.windows
                    joined = proposal.top[proposal.left_rank - 1]
                    if joined in b.windows:
                        route["visible_composite"] += 1
                        route["max_visible_composite_chars"] = max(
                            route["max_visible_composite_chars"], len(joined.text)
                        )
                        route["visible_composites_exceeding_current_excerpt_limit"] += (
                            len(joined.text) > MAX_EVIDENCE_EXCERPT_CHARS
                        )
                else:
                    route["fallback_due_to_missing_original_text"] += 1
                if budget not in examples and a.text != b.text and proposal.top[-1] in b.windows:
                    joined = proposal.top[proposal.left_rank - 1]
                    examples[budget] = {
                        "query": q,
                        "original_pair_ranks": [proposal.left_rank, proposal.right_rank],
                        "source": joined.doc,
                        "revision": joined.revision,
                        "locator": joined.locator,
                        "refill_source": proposal.top[-1].doc,
                        "refill_original_rank": proposal.refill_rank,
                    }
            route["different_serialized_messages"] += a.text != b.text
            route["checked_original_displayed_intervals"] += len(a.windows)
            if not retains(a.windows, b.windows):
                raise ValueError("An adopted or fallback message lost baseline source coverage")
            observed = {}
            for arm, message in (("A", a), ("B", b)):
                counts = totals[arm, budget]
                counts["visible_chunks"] += len(message.windows)
                counts["displayed_body_estimated_tokens"] += message.body_units
                counts["serialized_user_message_estimated_tokens"] += EST.estimate(message.text)
                counts["max_serialized_user_message_estimated_tokens"] = max(
                    counts["max_serialized_user_message_estimated_tokens"],
                    EST.estimate(message.text),
                )
                seen = outcomes(message.windows, gold, texts, official[q] if official else None)
                observed[arm] = seen
                record_coverage(counts, seen, gold)
                ids = [[w.chunk, w.doc, w.revision, w.start, w.end] for w in message.windows]
                projections[arm, budget].update(
                    (json.dumps([q, ids], separators=(",", ":")) + "\n").encode()
                )
                cite_digests[arm, budget].update(encoded([q, sha(message.text.encode())]) + b"\n")
            changed = delta(observed["A"], observed["B"], paired[budget])
            if any(v["lost"] for v in changed.values()):
                raise ValueError("Source coverage guard failed a labeled retention invariant")
            if changed:
                changes.append({"query_id": q, "budget": budget, **changed})
    for (arm, budget), total in totals.items():
        total["macro_source_recall"] = round(total["macro_source_recall"] / len(ranks), 9)
        if arm == "A":
            earlier = reference["metrics"][str(budget)]["A"]
            if any(total[k] != earlier[k] for k in total.keys() & earlier.keys()):
                raise ValueError("Baseline outcomes diverge from the prior study")
            if (
                projections[arm, budget].hexdigest()
                != reference["visible_source_span_sequence_sha256"][str(budget)][arm]
            ):
                raise ValueError("Baseline windows diverge from the previous archive replay")
    return {
        "provenance": provenance,
        "denominators": reference["denominators"],
        "admission": {
            str(b): {
                k: admission[b][k]
                for k in (
                    "eligible_proposals",
                    "accepted",
                    "fallback_due_to_missing_original_text",
                    "different_serialized_messages",
                    "visible_refill",
                    "visible_composite",
                    "max_visible_composite_chars",
                    "visible_composites_exceeding_current_excerpt_limit",
                    "checked_original_displayed_intervals",
                )
            }
            for b in BUDGETS
        },
        "metrics": {str(b): {a: totals[a, b] for a in ("A", "B")} for b in BUDGETS},
        "paired_to_A": {str(b): paired[b] for b in BUDGETS},
        "source_span_sequence_sha256": {
            str(b): {a: projections[a, b].hexdigest() for a in ("A", "B")} for b in BUDGETS
        },
        "serialized_user_messages_sha256": {
            str(b): {a: cite_digests[a, b].hexdigest() for a in ("A", "B")} for b in BUDGETS
        },
        "changed_gold": changes,
        "verified_composite_examples": examples,
    }


def check_policy():
    def window(k, lo, hi, doc="d", revision="r", text="abcdefgh"):
        loc = {"type": "text_range", "char_start": lo, "char_end": hi}
        return Window(k, doc, revision, lo, hi, text[lo:hi], loc)

    a, b = window("a", 0, 4), window("b", 4, 8)
    both = merge(a, b, "abcdefgh")
    assert both.text == "abcdefgh" and len(both.locator["components"]) == 2
    assert (
        merge(b, a, "abcdefgh").text == both.text
        and merge(a, window("c", 2, 5), "abcdefgh").text == "abcde"
    )
    assert not merge(a, window("c", 5, 8), "abcdefgh") and not merge(
        a, window("c", 2, 5, revision="s"), "abcdefgh"
    )
    assert not merge(a, window("c", 2, 5, doc="x"), "abcdefgh") and not merge(
        a, b, "abcdefgh"[::-1]
    )
    assert not merge(a, window("c", 2, 5, revision=None), "abcdefgh")
    assert retains([a, b], [both]) and not retains([a, b], [a])
    unknown = window("u", 0, 2, revision=None)
    assert retains([unknown], [unknown]) and not retains([unknown], [a])
    other = [window(str(i), 0, 2, doc=str(i)) for i in range(7)]
    pool = [a, *other[:3], b, other[3], a, other[4], other[5]]
    chosen = propose(pool, {w.doc: "abcdefgh" for w in pool})
    assert (chosen.left_rank, chosen.right_rank, chosen.refill_rank) == (1, 5, 8)
    assert len(chosen.top) == 6 and not propose(pool[:6], {w.doc: "abcdefgh" for w in pool})


async def run(output):
    if sha((HERE / "protocol.md").read_bytes().replace(b"\r\n", b"\n")) != PROTOCOL:
        raise ValueError("Registered protocol changed")
    previous = PREVIOUS.read_bytes().replace(b"\r\n", b"\n")
    if sha(previous) != "122aee9b32a690842e76e8109ef954c2b4b69313d418db0cc6ff1a732bbc199b":
        raise ValueError("Original baseline projection changed")
    baseline = json.loads(previous)
    check_policy()
    builder = "\n".join(inspect.getsource(fn) for fn in (_select_evidence, _evidence_prompt))
    result = {
        "protocol_lf_sha256": PROTOCOL,
        "baseline_projection_lf_sha256": sha(previous),
        "message_builder_lf_sha256": sha(builder.encode()),
        "current_api_citation_excerpt_limit_chars": MAX_EVIDENCE_EXCERPT_CHARS,
        "model_or_provider_calls": 0,
    }
    result["scifact_train"] = evaluate("scifact", scifact(), baseline["scifact_train"])
    result["fiqa_test"] = evaluate("fiqa", await fiqa(), baseline["fiqa_test"])
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_bytes(
        (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
    )
    print(json.dumps({k: v["admission"] for k, v in result.items() if isinstance(v, dict)}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=HERE / "results")
    asyncio.run(run(p.parse_args().output))
