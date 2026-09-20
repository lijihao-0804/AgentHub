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

from benchmarks.retrieval.corpus import chunk_ids_by_section
from benchmarks.retrieval.schema import load_dataset
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
    source_split: str | None = None,
    tags: list[str],
    ordinal: int,
) -> dict[str, Any]:
    provenance = {"source_kind": source_kind, "source_id": source_id}
    if source_split is not None:
        provenance["source_split"] = source_split
    return {
        "case_key": case_key,
        "split": split.upper(),
        "category": category,
        "input": input_value,
        "expected": expected,
        "tags": tags,
        "source_provenance": provenance,
        "ordinal": ordinal,
    }


def _retrieval_items(start_ordinal: int) -> list[dict[str, Any]]:
    dataset = load_dataset(PROJECT_ROOT / "benchmarks" / "retrieval" / "dataset.json")
    corpus = {
        (document.document_key, document.revision_key, section_key): chunk_ids
        for document in dataset.corpus
        for section_key, chunk_ids in chunk_ids_by_section(document).items()
    }
    items: list[dict[str, Any]] = []
    for index, case in enumerate(dataset.cases):
        relevant = [
            chunk_id
            for ground_truth in case.ground_truth
            for chunk_id in corpus[
                (
                    ground_truth.document_key,
                    ground_truth.revision_key,
                    ground_truth.locator["section_key"],
                )
            ]
        ]
        items.append(
            _item(
                case_key=f"m3-{case.case_id}",
                split=case.split,
                category="RETRIEVAL",
                input_value={"query": case.query},
                expected={"relevant_chunk_ids": relevant},
                source_kind="historical_benchmark",
                source_id=f"m3-retrieval-v1:{case.case_id}",
                source_split=case.split.upper(),
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
            expected = {
                "decision": "UNAVAILABLE",
                "approval_required": True,
                "failure_code": case["expected"]["failure_code"],
            }
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
                source_split=case["split"].upper(),
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
                split=case["split"],
                category="APPROVAL",
                input_value={"action": f"Approval sequence {case['case_id']}: {actions}."},
                expected={
                    "decision": decision,
                    "approval_required": True,
                    "execution_status": case["expected"]["execution_status"],
                    "run_status": case["expected"]["run_status"],
                    "ticket_count": case["expected"]["ticket_count"],
                    "execution_calls": case["expected"]["execution_calls"],
                    "authorization_ok": case["expected"]["authorization_ok"],
                    "resume_success": case["expected"]["resume_success"],
                },
                source_kind="historical_benchmark",
                source_id=f"m5-approval-runtime-v1:{case['case_id']}",
                source_split=case["split"].upper(),
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
                # The historical M6 file has no split field.  Keep all cases in DEV rather
                # than inventing a HOLDOUT assignment to satisfy the unified ratio.
                split="DEV",
                category="FAILURE",
                input_value={"scenario": case["scenario"]},
                expected={
                    "status": case["expected_run_status"],
                    "failure_code": case["expected_failure_code"],
                    "failure_category": case["expected_failure_category"],
                    "runtime_path": case["runtime_path"],
                },
                source_kind="historical_benchmark",
                source_id=f"m6-observability-failure-v1:{case['case_id']}",
                tags=["historical", "m6", "failure"],
                ordinal=start_ordinal + index,
            )
        )
    return items


def _curated_items(start_ordinal: int) -> list[dict[str, Any]]:
    retrieval_dataset = load_dataset(PROJECT_ROOT / "benchmarks" / "retrieval" / "dataset.json")
    sections = {
        (document.document_key, document.revision_key, section.section_key): section.text
        for document in retrieval_dataset.corpus
        for section in document.sections
    }
    section_chunks = {
        (document.document_key, document.revision_key, section_key): chunk_ids
        for document in retrieval_dataset.corpus
        for section_key, chunk_ids in chunk_ids_by_section(document).items()
    }
    items: list[dict[str, Any]] = []
    ordinal = start_ordinal
    qa_cases = [*retrieval_dataset.cases[:10], *retrieval_dataset.cases[20:25]]
    for index, case in enumerate(qa_cases):
        ground_truth = case.ground_truth[0]
        section_key = ground_truth.locator["section_key"]
        corpus_key = (ground_truth.document_key, ground_truth.revision_key, section_key)
        items.append(
            _item(
                case_key=f"curated-qa-{index + 1:02d}",
                split=case.split,
                category="KNOWLEDGE_QA",
                input_value={"question": case.query},
                expected={
                    "answer": sections[corpus_key],
                    "citations": list(section_chunks[corpus_key]),
                },
                source_kind="m7h_curated",
                source_id=f"m7h-qa-{index + 1:02d}",
                tags=["curated", "m7-h", "knowledge-qa"],
                ordinal=ordinal,
            )
        )
        ordinal += 1
    no_answer_questions = [
        "What is the exact annual leave carryover limit after the calendar year ends?",
        "On which payroll date are approved travel reimbursements deposited?",
        "How many remote-work days may each employee use per month?",
        "How often must an employee rotate a company service passphrase?",
        "How many business days does Billing take to settle an approved refund?",
        "What is the guaranteed resolution deadline for a P1 support case?",
        "Which insurance certificate expiry date is required for every supplier renewal?",
        "What is the maximum attachment size for a service ticket?",
        "How many hours may emergency data access remain active?",
        "What tax percentage applies to the bilingual invoice correction process?",
    ]
    for index, question in enumerate(no_answer_questions):
        items.append(
            _item(
                case_key=f"curated-no-answer-{index + 1:02d}",
                split="DEV" if index < 6 else "HOLDOUT",
                category="NO_ANSWER",
                input_value={"question": question},
                expected={
                    "answer": "The M3 corpus does not state this information.",
                    "answerable": False,
                },
                source_kind="m7h_curated",
                source_id=f"m7h-no-answer-{index + 1:02d}",
                tags=["curated", "m7-h", "no-answer"],
                ordinal=ordinal,
            )
        )
        ordinal += 1
    tool_tasks = [
        ("Calculate the business-day window from 09:30 to 17:30 in hours.", "8"),
        ("Calculate the annual leave balance after adding 12 monthly units.", "12 + 1"),
        ("Calculate the reimbursable meal total for 3 receipts of 25.", "3 * 25"),
        ("Calculate the P1 response target in seconds from fifteen minutes.", "15 * 60"),
        ("Calculate the number of days in the stated travel claim deadline.", "30"),
        ("Calculate the total of two approved expense amounts, 40 and 15.", "40 + 15"),
        ("Calculate the priority score represented by three normal support cases.", "3 * 1"),
        ("Calculate the handover review count for two leave dates and one contact.", "2 + 1"),
        ("Calculate the combined count of requester and owner fields in an intake record.", "2"),
    ]
    for index, (request, expression) in enumerate(tool_tasks):
        items.append(
            _item(
                case_key=f"curated-tool-{index + 1:02d}",
                split="DEV" if index < 7 else "HOLDOUT",
                category="TOOL",
                input_value={"request": request},
                expected={
                    "tool_identity": "calculator",
                    "arguments": {"expression": expression},
                    "tool_sequence": ["calculator"],
                },
                source_kind="m7h_curated",
                source_id=f"m7h-tool-{index + 1:02d}",
                tags=["curated", "m7-h", "tool"],
                ordinal=ordinal,
            )
        )
        ordinal += 1
    multi_step_tasks = [
        (
            "Find the collaboration window and calculate its duration.",
            ["search_knowledge", "calculator"],
        ),
        (
            "Find the annual-leave approval rule and calculate a two-month accrual.",
            ["search_knowledge", "calculator"],
        ),
        (
            "Find the P1 response target and convert fifteen minutes to seconds.",
            ["search_knowledge", "calculator"],
        ),
        (
            "Find the supplier renewal rule and calculate two review checkpoints.",
            ["search_knowledge", "calculator"],
        ),
        (
            "Find the ticket closure rule and calculate three recorded follow-ups.",
            ["search_knowledge", "calculator"],
        ),
        (
            "Find the least-privilege rule and calculate two separately reviewed effects.",
            ["search_knowledge", "calculator"],
        ),
    ]
    for index, (task, steps) in enumerate(multi_step_tasks):
        items.append(
            _item(
                case_key=f"curated-multi-step-{index + 1:02d}",
                split="DEV" if index < 4 else "HOLDOUT",
                category="MULTI_STEP",
                input_value={"task": task},
                expected={"steps": steps, "terminal_status": "SUCCEEDED"},
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
