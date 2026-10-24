"""Offline source-visibility attribution, never a runtime gold-based policy.

Trace original support annotations through actual selector and answer payloads.
Paragraph presence and literal answer presence are NOT semantic support scores.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.public_rag.scenario_runner import dump


def union_length(ranges):
    total, right = 0, -1
    for start, end in sorted(ranges):
        total += max(0, end - max(start, right))
        right = max(right, end)
    return total


def align_views(paragraphs, views, *, text_key, title_key):
    """Locators use list positions; official support annotations use paragraph idx."""
    if len({p["idx"] for p in paragraphs}) != len(paragraphs):
        raise ValueError("duplicate original paragraph identity")
    output = {}
    for view in views:
        loc, text = view["locator"], view[text_key]
        position, start, end = (loc.get(k) for k in ("paragraph_ordinal", "start", "end"))
        if any(type(v) is not int for v in (position, start, end)) or not 0 <= position < len(
            paragraphs
        ):
            raise ValueError("invalid original paragraph locator")
        paragraph = paragraphs[position]
        raw = paragraph["paragraph_text"]
        if (
            type(text) is not str
            or not text
            or not 0 <= start < end <= len(raw)
            or start + len(text) > end
            or text != raw[start : start + len(text)]
            or view.get(title_key) != paragraph["title"]
        ):
            raise ValueError("view is not the exact indicated original source range")
        output.setdefault(paragraph["idx"], []).append(
            {
                "start": start,
                "end": start + len(text),
                "text": text,
            }
        )
    return output


def trace_case(case, candidate_views):
    paragraphs = case["all_original_paragraphs"]
    by_idx = {p["idx"]: p for p in paragraphs}
    stages = {
        "selector_input": align_views(
            paragraphs, candidate_views, text_key="text", title_key="title"
        ),
        "answer_input": align_views(
            paragraphs, case["visible_records"], text_key="snippet", title_key="document_name"
        ),
    }
    steps = []
    for step in case["source_decomposition"]:
        idx = step["paragraph_support_idx"]
        if idx not in by_idx:
            raise ValueError("official step points to a missing original paragraph")
        paragraph, answer = by_idx[idx], step["answer"]
        observed = {}
        for name, mapped in stages.items():
            views = mapped.get(idx, [])
            observed[name] = {
                "paragraph_present": bool(views),
                "original_text_fraction": union_length([(v["start"], v["end"]) for v in views])
                / len(paragraph["paragraph_text"]),
                "literal_answer_present": bool(answer)
                and any(answer.casefold() in v["text"].casefold() for v in views),
                "visible_ranges": [{"start": v["start"], "end": v["end"]} for v in views],
            }
        steps.append(
            {
                "subquestion": step["question"],
                "annotated_answer": answer,
                "paragraph_idx": idx,
                "paragraph_title": paragraph["title"],
                "literal_answer_in_original_support": bool(answer)
                and answer.casefold() in paragraph["paragraph_text"].casefold(),
                "stages": observed,
            }
        )
    return {
        "query_id": case["query_id"],
        "baseline": case["baseline"],
        "question": case["question"],
        "official_aliases": case["official_aliases"],
        "steps": steps,
        "source_ranges_verified": True,
        "selector_view_count": len(candidate_views),
        "answer_view_count": len(case["visible_records"]),
        "semantic_support": ("UNASSESSED: presence/coverage/literal matching is not entailment"),
    }


def run(packet, events):
    expected = {c["query_id"] for c in packet["cases"]}
    dispatched = {}
    for event in events:
        if event["event"] != "dispatch" or event["query_id"] not in expected:
            continue
        qid = event["query_id"]
        if qid in dispatched:
            raise ValueError("multiple selector dispatches for one audit question")
        dispatched[qid] = json.loads(event["messages"][1]["content"])["candidates"]
    if set(dispatched) != expected or len(expected) != len(packet["cases"]):
        raise ValueError("full fixed audit and actual selector payloads required")
    cases = [trace_case(c, dispatched[c["query_id"]]) for c in packet["cases"]]
    return {
        "scope": __doc__,
        "questions": len(cases),
        "new_api_calls": 0,
        "source_range_views_verified": sum(
            c["selector_view_count"] + c["answer_view_count"] for c in cases
        ),
        "cases": cases,
        "limitations": [
            "Exposed, stratified audit sample; not dataset-wide prevalence or a new quality score.",
            "Gold decomposition/support indexes used ONLY offline; never exposed to online models.",
            "Original paragraph coverage and literal answer presence "
            "cannot prove semantic support.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("packet", "selector-events", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("refusing to overwrite source attribution")
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    events = [json.loads(s) for s in args.selector_events.read_text(encoding="utf-8").splitlines()]
    result = run(packet, events)
    dump(args.output, result)
    print(
        json.dumps(
            {k: result[k] for k in ("questions", "new_api_calls", "source_range_views_verified")}
        )
    )


if __name__ == "__main__":
    main()
