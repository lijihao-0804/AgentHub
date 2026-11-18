"""Compare fixed sixth-slot source diversity against exact redundant-span retention."""

import argparse
import asyncio
import hashlib
import inspect
import json
from collections import Counter, defaultdict
from pathlib import Path

from packages.knowledge.citation_qa import _evidence_prompt, _select_evidence
from packages.knowledge.contracts import RetrievedEvidence

from .inputs import Window, fiqa, scifact

HERE = Path(__file__).resolve().parent
PROTOCOL = "5f37d121c6b5d82bae0dffa45471e1951f2c31b986881a9d7bd46ded4e015a02"
ARMS = ("A", "B", "C")
BUDGETS = (512, 16000)


def covers(start, end, spans):
    """Every character must be covered; adjacent intervals may meet without overlap."""
    for lo, hi in sorted(spans):
        if lo > start:
            return False
        start = max(start, hi)
        if start >= end:
            return True
    return False


def policies(ranked):
    first = ranked[:6]
    choices = dict.fromkeys(ARMS, first)
    if len(first) < 6:
        return choices
    prefix, last = first[:-1], first[-1]
    sources = {w.doc for w in prefix}
    if last.doc not in sources:
        return choices
    replacement = next((w for w in ranked[6:20] if w.doc not in sources), None)
    if replacement is None:
        return choices
    choices["B"] = [*prefix, replacement]
    prior = [(w.start, w.end) for w in prefix if (w.doc, w.revision) == (last.doc, last.revision)]
    if last.revision and covers(last.start, last.end, prior):
        choices["C"] = choices["B"]
    return choices


def visible(query, top, budget, source):
    evidence = tuple(
        RetrievedEvidence(
            w.doc,
            w.revision or "",
            w.chunk,
            f"{source}:{w.doc}",
            w.locator,
            w.text,
            0.0,
            None,
            {},
        )
        for w in top
    )
    items = _select_evidence(evidence, max_chars=budget)
    message = _evidence_prompt(query, items)
    by_id = {w.chunk: w for w in top}
    windows = [by_id[item.evidence.chunk_id] for item in items]
    for i, (item, w) in enumerate(zip(items, windows, strict=True), 1):
        loc = json.dumps(w.locator, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        serialized = f"\n[{i}]\nsource: {source}:{w.doc}\nlocator: {loc}\ntext:\n{w.text}"
        if item.text != w.text or serialized not in message:
            raise ValueError("Serialized user message lost a source or changed its text")
    if budget == 16000 and windows != top:
        raise ValueError("The control budget did not fit all six ranked chunks")
    return windows


def outcomes(windows, gold, texts, official):
    by_doc = defaultdict(list)
    for w in windows:
        by_doc[w.doc].append((w.start, w.end))
    result = {"source": gold & by_doc.keys()}
    if official is None:
        result["whole_source_proxy"] = {
            d for d in result["source"] if texts[d] and covers(0, len(texts[d]), by_doc[d])
        }
    else:
        seen = {
            key
            for key, sentences in official.items()
            if all(covers(a, b, by_doc[key[0]]) for a, b in sentences)
        }
        result["rationale_set"] = seen
        result["rationale_source"] = {key[0] for key in seen}
    return result


def evaluate(name, inputs):
    src, ranks, texts, official, provenance = inputs
    totals, paired, paired_queries = defaultdict(Counter), defaultdict(Counter), defaultdict(dict)
    denominators = Counter(queries=len(ranks))
    changed = Counter()
    paired_details = []
    projections = {(arm, b): hashlib.sha256() for arm in ARMS for b in BUDGETS}
    for qid, ranking in ranks.items():
        gold = {d for d, grade in src.qrels[qid].items() if grade > 0}
        if not gold:
            raise ValueError("No positive qrels for a registered retrieval question")
        denominators["gold_query_source_pairs"] += len(gold)
        annotation = official[qid] if official else None
        if annotation is not None:
            denominators["annotated_queries"] += bool(annotation)
            denominators["annotated_query_source_pairs"] += len({k[0] for k in annotation})
            denominators["unique_official_query_source_label_sentence_sets"] += len(annotation)
        else:
            denominators["nonempty_qrel_answer_documents"] += sum(bool(texts[d]) for d in gold)
        arms = policies(ranking)
        changed.update({a: v != arms["A"] for a, v in arms.items()})
        changed["duplicate_sixth_source"] += ranking[5].doc in {w.doc for w in ranking[:5]}
        changed["unknown_original_sixth_revision"] += not ranking[5].revision
        for budget in BUDGETS:
            before = {}
            for arm, top in arms.items():
                windows = visible(src.queries[qid], top, budget, name)
                identity = [[w.chunk, w.doc, w.revision, w.start, w.end] for w in windows]
                projections[arm, budget].update(
                    (json.dumps([qid, identity], separators=(",", ":")) + "\n").encode()
                )
                res = outcomes(windows, gold, texts, annotation)
                counter = totals[(arm, budget)]
                counter["visible_chunks"] += len(windows)
                counter["distinct_visible_sources_summed"] += len({w.doc for w in windows})
                counter["budget_skipped_whole_chunks"] += len(top) - len(windows)
                counter["sixth_slot_in_message"] += top[5] in windows
                counter["macro_source_recall"] += len(res["source"]) / len(gold)
                for metric, items in res.items():
                    counter[metric + "_pairs"] += len(items)
                    counter[metric + "_hit_queries"] += bool(items)
                    if arm == "A":
                        before[metric] = items
                        continue
                    added, lost = items - before[metric], before[metric] - items
                    pair = paired[(arm, budget)]
                    pair[metric + "_gained"] += len(added)
                    pair[metric + "_lost"] += len(lost)
                    pair[metric + "_hit_queries_gained"] += bool(items) and not before[metric]
                    pair[metric + "_hit_queries_lost"] += bool(before[metric]) and not items
                    if added or lost:
                        key = (arm, budget, qid)
                        paired_queries[key][metric] = {
                            "gained": sorted(added),
                            "lost": sorted(lost),
                        }
                        if "sixth_slot" not in paired_queries[key]:
                            old, new = arms["A"][5], top[5]
                            old_visible = visible(src.queries[qid], arms["A"], budget, name)
                            paired_queries[key]["sixth_slot"] = {
                                "original_rank": ranking.index(new) + 1,
                                "before": [
                                    old.doc,
                                    old.revision,
                                    old.start,
                                    old.end,
                                    old in old_visible,
                                ],
                                "after": [
                                    new.doc,
                                    new.revision,
                                    new.start,
                                    new.end,
                                    new in windows,
                                ],
                                "before_source_spans_in_first_five": [
                                    [w.start, w.end]
                                    for w in top[:5]
                                    if (w.doc, w.revision) == (old.doc, old.revision)
                                ],
                            }
    for (arm, budget, qid), metrics in sorted(paired_queries.items()):
        paired_details.append({"arm": arm, "budget": budget, "query_id": qid, **metrics})
    for counter in totals.values():
        counter["macro_source_recall"] = round(counter["macro_source_recall"] / len(ranks), 9)
    return {
        "provenance": provenance,
        "denominators": dict(denominators),
        "policy_triggers": dict(changed),
        "metrics": {str(b): {a: totals[a, b] for a in ARMS} for b in BUDGETS},
        "paired_to_A": {str(b): {a: paired[a, b] for a in ARMS[1:]} for b in BUDGETS},
        "visible_source_span_sequence_sha256": {
            str(b): {a: projections[a, b].hexdigest() for a in ARMS} for b in BUDGETS
        },
        "within_chunk_truncations": 0,
        "changed_gold": paired_details,
    }


def policy_checks():
    """Boundary cases absent from archives still exercise the safety gate."""

    def win(i, doc, revision, a, b):
        return Window(str(i), doc, revision, a, b, "x" * (b - a))

    w = [
        win(1, "d", "r", 0, 4),
        win(2, "d", "r", 4, 8),
        win(3, "b", "s", 0, 1),
        win(4, "c", "t", 0, 1),
        win(5, "e", "u", 0, 1),
        win(6, "d", "r", 2, 7),
        win(7, "b", "s", 5, 6),
        win(8, "f", "v", 0, 1),
    ]
    assert policies(w)["B"][-1] == policies(w)["C"][-1] == w[7]
    for x in (win(6, "d", "r", 2, 9), win(6, "d", None, 2, 7), win(6, "d", "other", 2, 7)):
        short = [*w[:5], x, *w[6:]]
        assert policies(short)["C"][-1] == x and policies(short)["B"][-1] == w[7]
    assert not covers(0, 5, [(0, 2), (3, 5)])
    assert policies(w[:6])["C"] == w[:6] and policies(w[:4])["B"] == w[:4]


async def run(output):
    digest = hashlib.sha256((HERE / "protocol.md").read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    if digest != PROTOCOL:
        raise ValueError("The registered sixth-slot policy protocol differs")
    policy_checks()
    builder = "\n".join(inspect.getsource(fn) for fn in (_select_evidence, _evidence_prompt))
    report = {
        "protocol_lf_sha256": digest,
        "message_builder_lf_sha256": hashlib.sha256(builder.encode()).hexdigest(),
        "model_or_provider_calls": 0,
    }
    report["scifact_train"] = evaluate("scifact", scifact())
    report["fiqa_test"] = evaluate("fiqa", await fiqa())
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_bytes(
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()
    )
    print(json.dumps({k: v["policy_triggers"] for k, v in report.items() if isinstance(v, dict)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "results")
    asyncio.run(run(parser.parse_args().output))
