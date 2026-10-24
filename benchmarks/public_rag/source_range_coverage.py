"""Offline source-coordinate coverage, independent of ranking order.

This supplements, never changes, the historical concatenated-substring metric.
Character coverage does not establish semantic sufficiency or answer support.
Ambiguous source alignments stay UNKNOWN instead of receiving guessed credit.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=128)
def _normalized(text):
    chars, positions = [], []
    for match in re.finditer(r"\S+", text):
        if chars:
            chars.append(" ")
            positions.append(match.start() - 1)
        chars.extend(match.group())
        positions.extend(range(match.start(), match.end()))
    return "".join(chars), positions


def locate_unique(article: str, fragment: str, *, bounds=None):
    """Align exact, case-preserved non-whitespace content in one declared scope."""
    start, end = bounds if bounds is not None else (0, len(article))
    if not 0 <= start <= end <= len(article):
        raise ValueError("source bounds outside article")
    haystack, positions = _normalized(article[start:end])
    needle, _ = _normalized(fragment)
    if not needle:
        return None
    offset = haystack.find(needle)
    if offset < 0 or haystack.find(needle, offset + 1) >= 0:
        return None
    return start + positions[offset], start + positions[offset + len(needle) - 1] + 1


def merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged = []
    for start, end in sorted(ranges):
        if start < 0 or end <= start:
            raise ValueError("invalid nonempty source interval")
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def covered_characters(required, visible):
    required, visible = merge_ranges(required), merge_ranges(visible)
    total = sum(end - start for start, end in required)
    covered = sum(
        max(0, min(right, end) - max(left, start))
        for left, right in required
        for start, end in visible
    )
    return {
        "required_chars": total,
        "covered_chars": covered,
        "fraction": covered / total if total else None,
        "complete": covered == total if total else None,
    }


def annotation_coverage(article, evidence_sets, records, sources, *, paper_id):
    """Measure each annotation separately; do not mix incompatible gold sets.

    Source identity and exact snippet-to-chunk/article alignment are mandatory.
    Repeated ambiguous text produces UNKNOWN. No new semantic labels are made.
    """
    ranges, errors = [], []
    for record in records:
        chunk_id, snippet = record.get("chunk_id"), record.get("snippet")
        source = sources.get(chunk_id)
        if not source or source["paper_id"] != paper_id or type(snippet) is not str:
            errors.append({"chunk_id": chunk_id, "reason": "UNKNOWN_OR_CROSS_PAPER_SOURCE"})
            continue
        chunk_range = locate_unique(article, source["chunk_text"])
        view_range = locate_unique(article, snippet, bounds=chunk_range) if chunk_range else None
        if view_range is None:
            errors.append({"chunk_id": chunk_id, "reason": "UNALIGNED_OR_AMBIGUOUS_VIEW"})
        else:
            ranges.append(view_range)
    annotations = []
    for evidence in evidence_sets:
        required = [locate_unique(article, text) for text in evidence if text.strip()]
        aligned = bool(required) and all(span is not None for span in required)
        valid = aligned and not errors
        annotations.append(
            {
                "status": "OBSERVED" if valid else "UNKNOWN",
                "gold_alignment": "UNIQUE" if aligned else "MISSING_OR_AMBIGUOUS",
                "coverage": covered_characters(required, ranges) if valid else None,
            }
        )
    return {
        "source_view_errors": errors,
        "visible_ranges": merge_ranges(ranges),
        "annotations": annotations,
        "semantic_sufficiency": "UNKNOWN",
        "measure": "character union in original source coordinates; not semantic entailment",
    }


def replay(source_root, run_name, output):
    """Read historical traces once and write a new, explicitly additive report."""
    from benchmarks.public_rag.paragraph_qa_live import article_and_spans

    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    if output.exists():
        raise FileExistsError("refusing to overwrite a coordinate report")
    data = read(source_root / "frozen-dataset.json")
    sources = read(source_root / "source-map.json")
    rows = {r["query_id"]: r for r in read(source_root / run_name / "run.json")}
    articles = {p["paper_id"]: article_and_spans(p)[0] for p in data["corpus"]}
    values = []
    for case in data["cases"]:
        evidence = [
            a["evidence"] for a in case["annotations"] if a["answer_type"] != "unanswerable"
        ]
        row = rows.get(case["query_id"], {})
        for arm in ("fixed", "structural"):
            result = row.get("arms", {}).get(arm, {})
            retrievals = result.get("retrievals", [])
            trace = retrievals[0]["trace"] if retrievals else {}
            stages = {}
            for name in ("dense", "sparse", "fusion", "selection", "rerank"):
                stage = trace.get(name)
                if stage is not None:
                    stages[name] = [r["chunk_id"] for r in stage["results"]]
            if "dense" in stages and "sparse" in stages:
                stages["candidate_union"] = list(dict.fromkeys(stages["dense"] + stages["sparse"]))
            if retrievals:
                stages["final"] = [r["chunk_id"] for r in retrievals[0]["evidence"]]
            # Full source index is an offline upper bound, not model-visible input.
            stages["all_index_chunks"] = [
                cid
                for cid, s in sources.items()
                if s["paper_id"] == case["paper_id"] and s["arm"] == arm
            ]
            measurements = {}
            for name, ids in stages.items():
                records = [{"chunk_id": cid, "snippet": sources[cid]["chunk_text"]} for cid in ids]
                measurements[name] = annotation_coverage(
                    articles[case["paper_id"]],
                    evidence,
                    records,
                    sources,
                    paper_id=case["paper_id"],
                )
            measurements["actual_model"] = annotation_coverage(
                articles[case["paper_id"]],
                evidence,
                result.get("visible_records", []),
                sources,
                paper_id=case["paper_id"],
            )
            values.append(
                {
                    "query_id": case["query_id"],
                    "paper_id": case["paper_id"],
                    "arm": arm,
                    "run_status": result.get("run_status", "NOT_ATTEMPTED"),
                    "stages": measurements,
                }
            )
    summary = {}
    for arm in ("fixed", "structural"):
        names = sorted({stage for v in values if v["arm"] == arm for stage in v["stages"]})
        summary[arm] = {}
        for stage in names:
            measures = [v["stages"].get(stage) for v in values if v["arm"] == arm]
            valid = [
                m
                for m in measures
                if m and any(a["coverage"] is not None for a in m["annotations"])
            ]
            complete = sum(
                any(a["coverage"] and a["coverage"]["complete"] for a in m["annotations"])
                for m in valid
            )
            summary[arm][stage] = {
                "registered": len(measures),
                "evaluable": len(valid),
                "complete_source_coordinate_union": complete,
                "unknown_or_no_positive_annotation": len(measures) - len(valid),
            }
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "scope": (
            "posthoc exposed development; additive coordinate diagnostic, not semantic sufficiency"
        ),
        "source_root": source_root.as_posix(),
        "run_name": run_name,
        "planned_cases": len(data["cases"]),
        "planned_arm_rows": len(values),
        "summary": summary,
        "cases": values,
        "new_model_requests": 0,
        "old_scores_changed": False,
    }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"planned_arm_rows": len(values), "summary": summary}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("benchmarks/public_rag/results/paragraph-qa-20261023"),
    )
    parser.add_argument("--run-name", default="live-run-2")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    replay(args.source_root, args.run_name, args.output)
