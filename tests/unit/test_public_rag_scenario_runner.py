from benchmarks.public_rag.scenario_runner import (
    budget_context,
    chunks,
    complete_text_ids,
    select_context,
    support_overlap_groups,
)


def test_neighbors_stay_in_section_and_keep_anchor_first():
    units = [
        {
            "document_id": str(i),
            "text": "Evidence",
            "ordinal": i,
            "title": "Paper",
            "section": "A" if i < 2 else "B",
        }
        for i in range(3)
    ]
    cs = chunks(units, True)
    expanded = select_context([cs[1]], cs, units, neighbors=True)
    assert [x["document_id"] for x in expanded] == ["1", "0"]


def test_full_coverage_merges_overlap_and_does_not_count_duplicate_spans():
    unit = {"document_id": "p", "text": "x" * 100}
    assert (
        complete_text_ids(
            [
                {"document_id": "p", "start": 0, "end": 50},
                {"document_id": "p", "start": 0, "end": 50},
            ],
            [unit],
        )
        == []
    )
    assert complete_text_ids(
        [
            {"document_id": "p", "start": 0, "end": 50},
            {"document_id": "p", "start": 45, "end": 100},
        ],
        [unit],
    ) == ["p"]


def test_context_policy_admits_exact_unicode_evidence_with_real_byte_cap():
    evidence = [{"document_id": "p", "body": "中文 evidence", "start": 0, "end": 11}]
    kept, budget = budget_context("Question?", evidence, cap=1000)
    assert kept == evidence
    assert budget["status"] == "ADMITTED"
    large = [{"document_id": "large", "body": "x" * 2000}]
    kept, _ = budget_context("Question?", large + evidence, cap=1000)
    assert kept == evidence


def test_reused_support_text_groups_cases_without_using_distractors():
    units = [
        {"document_id": name, "text": text}
        for name, text in [
            ("a", "same support"),
            ("b", "same  support"),
            ("c", "other"),
            ("d", "same support"),
        ]
    ]
    queries = [
        {"query_id": q, "evidence_sets": [[source]]}
        for q, source in [("q1", "a"), ("q2", "b"), ("q3", "c")]
    ]
    groups = support_overlap_groups(queries, units)
    assert groups["q1"] == groups["q2"]
    assert groups["q1"] != groups["q3"]
