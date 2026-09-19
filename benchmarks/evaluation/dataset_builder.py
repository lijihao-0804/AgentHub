"""Build and verify the frozen M7-H unified evaluation dataset.

The builder intentionally imports the historical JSON artifacts as data only.  It does not
modify them; every normalized item keeps a source provenance record so the unified artifact
remains auditable and reproducible.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from packages.evaluation.validation import dataset_content_hash, validate_dataset_items

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_VERSION = "m7-unified-evaluation-v1"
SCHEMA_VERSION = 1
DEFAULT_OUTPUT = PROJECT_ROOT / "benchmarks" / "evaluation" / "dataset.json"


def _read(relative: str) -> dict[str, Any]:
    return json.loads((PROJECT_ROOT / relative).read_text(encoding="utf-8"))


def _item(
    *,
    case_key: str,
    split: str,
    category: str,
    input_value: dict[str, Any],
    expected: dict[str, Any],
    source_kind: str,
    source_id: str,
    tags: list[str],
    ordinal: int,
) -> dict[str, Any]:
    return {
        "case_key": case_key,
        "split": split.upper(),
        "category": category,
        "input": input_value,
        "expected": expected,
        "tags": tags,
        "source_provenance": {"source_kind": source_kind, "source_id": source_id},
        "ordinal": ordinal,
    }


def _retrieval_items(start_ordinal: int) -> list[dict[str, Any]]:
    raw = _read("benchmarks/retrieval/dataset.json")
    corpus = {
        (
            document["document_key"],
            section["section_key"],
        ): f"m3-{document['document_key']}-{section['section_key']}"
        for document in raw["corpus"]
        for section in document["sections"]
    }
    items: list[dict[str, Any]] = []
    for index, case in enumerate(raw["cases"]):
        relevant = [
            corpus[(ground_truth["document_key"], ground_truth["locator"]["section_key"])]
            for ground_truth in case["ground_truth"]
        ]
        items.append(
            _item(
                case_key=f"m3-{case['id']}",
                split=case["split"],
                category="RETRIEVAL",
                input_value={"query": case["query"]},
                expected={"relevant_chunk_ids": relevant},
                source_kind="historical_benchmark",
                source_id=f"m3-retrieval-v1:{case['id']}",
                tags=["historical", "m3", "retrieval"],
                ordinal=start_ordinal + index,
            )
        )
    return items


def _legacy_items(start_ordinal: int) -> list[dict[str, Any]]:
    raw = _read("benchmarks/agent_runtime/dataset.json")["cases"]
    items: list[dict[str, Any]] = []
    ordinal = start_ordinal
    for case in raw:
        if case["category"] == "tool_selection":
            first = next(step for step in case["model_script"] if step["type"] == "TOOL_CALL")
            expected = {
                "tool_identity": first["name"],
                "arguments": first["arguments"],
                "tool_sequence": case["expected"]["tool_sequence"],
            }
            category = "TOOL"
        elif case["category"] == "multi_step_read":
            expected = {
                "steps": case["expected"]["tool_sequence"],
                "terminal_status": "SUCCEEDED",
            }
            category = "MULTI_STEP"
        elif case["category"] == "approval_unavailable":
            expected = {"decision": "PENDING", "approval_required": True}
            category = "APPROVAL"
        else:
            continue
        items.append(
            _item(
                case_key=f"m4-{case['case_id']}",
                split=case["split"],
                category=category,
                input_value={
                    "request"
                    if category == "TOOL"
                    else "task"
                    if category == "MULTI_STEP"
                    else "action": case["input"]
                },
                expected=expected,
                source_kind="historical_benchmark",
                source_id=f"m4-agent-runtime-v1:{case['case_id']}",
                tags=["historical", "m4", category.lower()],
                ordinal=ordinal,
            )
        )
        ordinal += 1
    return items


def _approval_items(start_ordinal: int) -> list[dict[str, Any]]:
    raw = _read("benchmarks/approval_runtime/dataset.json")["cases"]
    selected = raw[:7]
    items: list[dict[str, Any]] = []
    for index, case in enumerate(selected):
        actions = ", ".join(case["actions"])
        decision = case["expected"]["decision_status"]
        items.append(
            _item(
                case_key=f"m5-{case['case_id']}",
                split="DEV" if index < 5 else "HOLDOUT",
                category="APPROVAL",
                input_value={"action": f"Approval sequence {case['case_id']}: {actions}."},
                expected={"decision": decision, "approval_required": True},
                source_kind="historical_benchmark",
                source_id=f"m5-approval-runtime-v1:{case['case_id']}",
                tags=["historical", "m5", "approval"],
                ordinal=start_ordinal + index,
            )
        )
    return items


def _failure_items(start_ordinal: int) -> list[dict[str, Any]]:
    raw = _read("benchmarks/observability/dataset.json")["cases"]
    items: list[dict[str, Any]] = []
    for index, case in enumerate(raw):
        items.append(
            _item(
                case_key=f"m6-{case['case_id']}",
                split="DEV" if index < 8 else "HOLDOUT",
                category="FAILURE",
                input_value={"scenario": case["scenario"]},
                expected={
                    "status": case["expected_run_status"],
                    "failure_code": case["expected_failure_code"],
                },
                source_kind="historical_benchmark",
                source_id=f"m6-observability-failure-v1:{case['case_id']}",
                tags=["historical", "m6", "failure"],
                ordinal=start_ordinal + index,
            )
        )
    return items


def _curated_items(start_ordinal: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    ordinal = start_ordinal
    for index in range(15):
        items.append(
            _item(
                case_key=f"curated-qa-{index + 1:02d}",
                split="DEV" if index < 10 else "HOLDOUT",
                category="KNOWLEDGE_QA",
                input_value={
                    "question": (
                        f"Which approved knowledge fact does curated QA case {index + 1} ask about?"
                    )
                },
                expected={
                    "answer": f"Curated knowledge answer {index + 1}.",
                    "citations": [f"curated-chunk-{index + 1:02d}"],
                },
                source_kind="m7h_curated",
                source_id=f"m7h-qa-{index + 1:02d}",
                tags=["curated", "m7-h", "knowledge-qa"],
                ordinal=ordinal,
            )
        )
        ordinal += 1
    for index in range(10):
        items.append(
            _item(
                case_key=f"curated-no-answer-{index + 1:02d}",
                split="DEV" if index < 7 else "HOLDOUT",
                category="NO_ANSWER",
                input_value={
                    "question": (
                        "Which unpublished internal fact is absent from curated corpus "
                        f"case {index + 1}?"
                    )
                },
                expected={
                    "answer": (
                        "The corpus does not contain enough information to answer this question."
                    )
                },
                source_kind="m7h_curated",
                source_id=f"m7h-no-answer-{index + 1:02d}",
                tags=["curated", "m7-h", "no-answer"],
                ordinal=ordinal,
            )
        )
        ordinal += 1
    for index in range(9):
        items.append(
            _item(
                case_key=f"curated-tool-{index + 1:02d}",
                split="DEV" if index < 7 else "HOLDOUT",
                category="TOOL",
                input_value={
                    "request": (
                        f"Use calculator for curated expression {index + 1}: "
                        f"{index + 2} + {index + 3}."
                    )
                },
                expected={
                    "tool_identity": "calculator",
                    "arguments": {"expression": f"{index + 2} + {index + 3}"},
                    "tool_sequence": ["calculator"],
                },
                source_kind="m7h_curated",
                source_id=f"m7h-tool-{index + 1:02d}",
                tags=["curated", "m7-h", "tool"],
                ordinal=ordinal,
            )
        )
        ordinal += 1
    for index in range(6):
        items.append(
            _item(
                case_key=f"curated-multi-step-{index + 1:02d}",
                split="DEV" if index < 4 else "HOLDOUT",
                category="MULTI_STEP",
                input_value={
                    "task": f"Complete curated workflow {index + 1} using lookup then calculation."
                },
                expected={"steps": ["lookup", "calculator"], "terminal_status": "SUCCEEDED"},
                source_kind="m7h_curated",
                source_id=f"m7h-multi-step-{index + 1:02d}",
                tags=["curated", "m7-h", "multi-step"],
                ordinal=ordinal,
            )
        )
        ordinal += 1
    return items


def build_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    items.extend(_retrieval_items(0))
    items.extend(_legacy_items(len(items)))
    items.extend(_approval_items(len(items)))
    items.extend(_failure_items(len(items)))
    items.extend(_curated_items(len(items)))
    return validate_dataset_items(items)


def build_payload() -> dict[str, Any]:
    return {
        "dataset_version": DATASET_VERSION,
        "schema_version": SCHEMA_VERSION,
        "items": build_items(),
    }


def summary(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload["items"]
    return {
        "dataset_version": payload["dataset_version"],
        "schema_version": payload["schema_version"],
        "dataset_hash": dataset_content_hash(items, schema_version=payload["schema_version"]),
        "case_count": len(items),
        "split_counts": dict(sorted(Counter(item["split"] for item in items).items())),
        "category_counts": dict(sorted(Counter(item["category"] for item in items).items())),
        "source_counts": dict(
            sorted(Counter(item["source_provenance"]["source_kind"] for item in items).items())
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = build_payload()
    if args.check:
        actual = json.loads(args.output.read_text(encoding="utf-8"))
        if actual != expected:
            raise SystemExit("M7-H dataset drift detected; regenerate the committed artifact.")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(summary(expected), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DATASET_VERSION", "SCHEMA_VERSION", "build_items", "build_payload", "main"]
