"""Adversarial fixtures for attribution in the bilingual refusal experiment. No provider traffic."""

import json
from copy import deepcopy

import httpx
import pytest

from benchmarks.retrieval.experiments.bilingual_refusal_100 import review
from benchmarks.retrieval.experiments.bilingual_refusal_100.evidence import (
    observations,
    stage_coverage,
)
from benchmarks.retrieval.experiments.bilingual_refusal_100.metrics import summarize
from benchmarks.retrieval.experiments.bilingual_refusal_100.review import (
    blinded_unit,
    validate_verdict,
)
from benchmarks.retrieval.experiments.bilingual_refusal_100.runner import (
    GuardedProviderTransport,
    order,
    project,
    smoke_material,
    wire_guard,
)


def recorded():
    cases, _, scorer, covers = smoke_material()
    case = cases[0]
    doc = {"snapshot": "snap", "revision": "rev", "document": "doc"}
    item = {
        "id": 1,
        "doc_id": case["doc_id"],
        "text": "The city named in this note is Oslo.",
        "chunk_id": "chunk",
        "locator": {"ordinal": 0},
    }
    text = "Oslo [1]."
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": f"Question:\n{case['question']}\n\n[1]\n{item['text']}"},
    ]
    event = {
        "attempt": 1,
        "messages": messages,
        "finish_reason": "stop",
        "content": json.dumps(
            {"answer": text, "citation_ids": [1], "insufficient_evidence": False}
        ),
    }
    ids = {name: ["chunk"] for name in ("dense", "sparse", "fusion", "selection", "rerank")}
    returned = {
        "evidence": [item],
        "stages": ids,
        "http": 200,
        "chunks": {"chunk": {k: item[k] for k in ("doc_id", "locator", "text")}},
        "body": {
            "retrieval_trace": {
                "snapshot_id": "snap",
                "stages": {
                    ("fused" if name == "fusion" else name): {"results": [{"chunk_id": "chunk"}]}
                    for name in ids
                },
            },
            "insufficient_evidence": False,
            "answer": text,
            "citations": [
                {
                    "id": 1,
                    "document_id": "doc",
                    "document_revision_id": "rev",
                    "chunk_id": "chunk",
                    "excerpt": item["text"],
                    "locator": item["locator"],
                }
            ],
        },
    }
    return case, doc, [event], returned, scorer, covers


def test_actual_endpoint_citation_and_last_response_are_bound():
    case, doc, events, returned, _, _ = recorded()
    first, final = observations(case, doc, events, returned, "system")
    assert first[0]["state"] == final["state"] == "answer"
    returned["body"]["citations"][0]["excerpt"] = "Another source says Oslo."
    with pytest.raises(ValueError, match="numbered evidence"):
        observations(case, doc, events, returned, "system")
    returned["body"]["answer"] = "The city is Paris [1]."
    with pytest.raises(ValueError, match="last provider output"):
        observations(case, doc, events, returned, "system")


def test_span_coverage_distinguishes_candidate_rerank_and_visible_loss():
    case, _, _, returned, _, covers = recorded()
    returned["chunks"]["other"] = {
        "doc_id": case["doc_id"],
        "text": "Other facts.",
        "locator": {"ordinal": 1},
    }
    returned["stages"]["fusion"].append("other")
    returned["stages"]["selection"].append("other")
    returned["stages"]["rerank"] = ["other"]
    returned["evidence"] = [{"id": 1, "chunk_id": "other", **returned["chunks"]["other"]}]
    coverage = stage_coverage(case, returned, covers)
    assert {k: v["gold_covered"] for k, v in coverage.items()} == {
        "candidate": True,
        "reranked": False,
        "presented": False,
    }
    returned["evidence"][0]["text"] = "An inserted passage mentioning Oslo."
    with pytest.raises(ValueError, match="actual scoped chunk"):
        stage_coverage(case, returned, covers)


def test_matched_evidence_and_wrong_language_have_separate_scores():
    case, doc, events, returned, scorer, covers = recorded()
    first_final = observations(case, doc, events, returned, "system")
    base = project(case, "baseline", events, returned, first_final, scorer, covers, offline=True)
    candidate = deepcopy(base)
    candidate["arm"] = "candidate"
    for phase in ("first", "final"):
        candidate[phase]["scores"].update(
            language_match=False,
            joint_correct=False,
            value_with_language=False,
            citation_with_language=False,
        )
    summary = summarize([base, candidate], smoke=True)
    en = summary["by_language"]["en"]
    assert en["paired_changes"]["first"]["strict"]["regressed"] == [case["id"]]
    assert en["paired_changes"]["first"]["semantic_and_citation"]["regressed"] == []
    assert summary["retrieval"]["presented"]["annotated_support_coverage"]["count"] == 1
    assert (
        summary["by_language"]["zh"]["retrieval"]["presented"]["annotated_support_coverage"][
            "total"
        ]
        == 0
    )
    candidate["retrieval"]["presented"]["gold_covered"] = False
    with pytest.raises(ValueError, match="identical actual evidence"):
        summarize([base, candidate], smoke=True)


@pytest.mark.asyncio
async def test_guard_blocks_drift_before_the_underlying_transport(monkeypatch):
    calls = []

    async def inert(transport, request):
        calls.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(GuardedProviderTransport, "handle_async_request", inert)
    captured = {"presented": {"prompt": "the actual numbered evidence", "identity": "digest"}}
    messages = [
        {"role": "system", "content": "requested policy"},
        {"role": "user", "content": "different evidence"},
    ]
    with wire_guard("requested policy", captured, None, []):
        with pytest.raises(ValueError, match="frozen question or policy"):
            await GuardedProviderTransport.handle_async_request(
                None,
                httpx.Request("POST", "https://benchmark.invalid", json={"messages": messages}),
            )
    assert not calls and captured["wire_blocked"]


def test_blind_review_requires_language_and_justification_for_failures():
    with pytest.raises(ValueError):
        validate_verdict({"value": True, "support": True, "reason": ""}, final=False)
    failure = {"value": True, "support": True, "language_match": False, "reason": ""}
    with pytest.raises(ValueError):
        validate_verdict(failure, final=False)
    failure.update(reason="Response is in a different language.", uncertain=True)
    validate_verdict(failure, final=False)
    with pytest.raises(ValueError):
        validate_verdict(failure, final=True)
    unresolved = {
        "value": None,
        "support": True,
        "language_match": True,
        "unresolved": True,
        "reason": "Both reviewers found the required value ambiguous.",
    }
    validate_verdict(unresolved, final=True)
    with pytest.raises(ValueError):
        validate_verdict(unresolved, final=False)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"answer":"The text stops mid-sent', "The text stops mid-sent"),
        (r'{"answer":"A \"quoted\" word and \u4e2d\u65', 'A "quoted" word and 中'),
        (r'{"answer":"A\ud83d\ude00\ud83d', "A😀"),
        (
            '{"reasoning_content":"private","tool_calls":[{"answer":"private"}],"answer":"visible"',
            "visible",
        ),
        ('```json\n{"answer":"visible fragment', "visible fragment"),
        ('{"tool_calls":[{"answer":"private"}]}', None),
        ('{"analysis":{"answer":"hidden', None),
        ('{"reasoning_content":"thinking about an answer', None),
        ('some commentary before {"answer":"not a top-level object"}', None),
        ('{"answer": ["not a text field"]}', None),
        ('{"answer":"first", "answer":"second"}', None),
    ],
)
def test_invalid_answer_fragment_is_only_used_for_blind_language_review(content, expected):
    invalid = {"state": "invalid", "answer": "", "citations": []}
    original = deepcopy(invalid)
    unit = blinded_unit(invalid, content)
    assert invalid == original
    assert blinded_unit(invalid) is None
    assert blinded_unit({"state": "error", "answer": "", "citations": []}, content) is None
    if expected is None:
        assert unit is None
    else:
        assert unit == {**original, "answer": expected, "answer_field_fragment": True}
    parsed = {"state": "refusal", "answer": "Recorded refusal.", "citations": []}
    assert blinded_unit(parsed, content) == parsed


def test_language_only_supplement_preserves_the_automatic_record(monkeypatch, tmp_path):
    case, doc, events, returned, scorer, covers = recorded()
    events[0]["content"] = '{"answer":"Visible prefix'
    returned.update(http=422, body={"error": {"code": "INVALID_MODEL_OUTPUT"}})
    results = observations(case, doc, events, returned, "system")
    automatic = project(case, "baseline", events, returned, results, scorer, covers, offline=True)
    frozen = deepcopy(automatic)
    unit = blinded_unit(results[0][0], events[0]["content"])
    mapping = {f"{case['id']}/baseline/first": "unit"}
    parts = [[{"outputs": [{"uid": "unit", **unit}]}], []]
    monkeypatch.setattr(review, "private", lambda path: None)
    monkeypatch.setattr(review, "packets", lambda *args: (parts, mapping, [deepcopy(automatic)]))
    review.dump(tmp_path / "mapping-private.json", mapping)
    for n, items in enumerate(parts, 1):
        review.dump(tmp_path / f"packet-{n}.json", items)
        review.dump(tmp_path / f"verdicts-{n}.json", {})
    verdict = {"value": False, "support": False, "language_match": True, "reason": "Readable text."}
    review.dump(tmp_path / "additional-verdicts.json", {"unit": verdict})
    output = tmp_path / "reviewed.jsonl"
    assert review.apply(tmp_path, tmp_path, output)["reviewed_units"] == 1
    row = review.rows(output)[0]
    assert row["first"]["scores"]["language_match"] is True
    assert not row["first"]["scores"]["joint_correct"] and row["first"]["state"] == "invalid"
    assert row["first"]["answer_sha256"] == frozen["first"]["answer_sha256"]
    assert row["first"]["wire_output_sha256"] == frozen["first"]["wire_output_sha256"]
    assert row["first"]["semantic_review"]["visible_answer_fragment_sha256"] == review.text_hash(
        "Visible prefix"
    )
    assert row["final"] == frozen["final"] and automatic == frozen
    (tmp_path / "verdicts-1.json").write_text(json.dumps({"unit": verdict}), encoding="utf8")
    with pytest.raises(ValueError, match="cannot replace"):
        review.apply(tmp_path, tmp_path, tmp_path / "duplicate.jsonl")


def test_registered_order_balances_each_stratum_and_the_overall_sample():
    sample = [
        {"language": lang, "answerable": pos}
        for lang in ("en", "zh")
        for pos in (True, False)
        for _ in range(25)
    ]
    leading = [arms[0] for _, arms in order(sample)]
    assert leading.count("baseline") == leading.count("candidate") == 50
    for index in range(0, 100, 25):
        assert leading[index : index + 25].count("baseline") in (12, 13)
