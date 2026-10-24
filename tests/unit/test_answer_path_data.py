from pathlib import Path

import pytest

from benchmarks.public_rag import answer_path_data


def _dataset():
    base = Path("benchmarks/public_rag/results/paragraph-qa-20261023")
    if not all(
        (base / name).is_file()
        for name in (
            "frozen-dataset.json",
            "source-map.json",
            "prepared.json",
            "live-run-2/run.json",
        )
    ):
        pytest.skip("requires the full local-only paragraph QA source and runtime artifacts")
    return answer_path_data.prepare_dataset()


def test_records_are_exact_rendered_source_slices():
    dataset = _dataset()
    summary = answer_path_data.validate_source_records(dataset)
    assert summary["cases"] == 18
    assert summary["natural_controls"] == 5
    frozen = answer_path_data._load_object(
        Path("benchmarks/public_rag/results/paragraph-qa-20261023/frozen-dataset.json")
    )
    assert answer_path_data.validate_source_slices(dataset, frozen) == {
        "records_checked": summary["records"]
    }
    run = answer_path_data._load_list(
        Path("benchmarks/public_rag/results/paragraph-qa-20261023/live-run-2/run.json")
    )
    by_id = {row["query_id"]: row for row in run}
    for case in dataset["cases"] + dataset["natural_controls"]:
        assert [r["text"] for r in case["conditions"]["actual"]["records"]] == [
            r["snippet"] for r in by_id[case["query_id"]]["arms"]["fixed"]["visible_records"]
        ]
    for case in dataset["cases"]:
        for condition in ("actual", "sufficient", "removed"):
            for record in case["conditions"][condition]["records"]:
                start_end = record["source_ranges"][0]
                assert (
                    record["text"]
                    == _render(record["paper_id"])[start_end["char_start"] : start_end["char_end"]]
                )
    for case in dataset["natural_controls"]:
        for record in case["conditions"]["actual"]["records"]:
            span = record["source_ranges"][0]
            assert (
                record["text"] == _render(record["paper_id"])[span["char_start"] : span["char_end"]]
            )


def test_deletion_is_exact_actual_order_subset():
    dataset = _dataset()
    for case in dataset["cases"]:
        actual = case["conditions"]["actual"]["records"]
        removed = case["conditions"]["removed"]["records"]
        removed_ids = case["curation"]["removed_source_ids"]
        removed_set = set(removed_ids)
        sufficient = case["conditions"]["sufficient"]["records"]
        assert removed == [r for r in sufficient if r["source_id"] not in removed_set]
        assert removed == [r for r in actual if r["source_id"] in {x["source_id"] for x in removed}]
        packet = {
            "paper_title": _paper(case["paper_id"])["title"],
            "records": [
                {"source_id": record["source_id"], "text": record["text"]}
                for record in case["conditions"]["sufficient"]["records"]
            ],
            "trust": "UNTRUSTED",
        }
        packet_bytes = len(
            answer_path_data.json.dumps(
                packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        assert ("PACKET_EXCEEDS_8000_UTF8_BYTES" in case["curation"]["notes"]) == (
            packet_bytes > answer_path_data._PACKET_LIMIT_BYTES
        )


def test_selection_does_not_read_previous_answers(monkeypatch):
    baseline = _dataset()
    original_list = answer_path_data._load_list

    def altered_run_loader(path):
        value = original_list(path)
        for row in value:
            row["arms"]["fixed"]["answer"] = {
                "answer": "DIFFERENT PREVIOUS ANSWER",
                "unanswerable": False,
                "cited_chunk_ids": ["DIFFERENT"],
            }
            row["arms"]["fixed"]["model_calls"] = [{"content": "DIFFERENT"}]
        return value

    monkeypatch.setattr(answer_path_data, "_load_list", altered_run_loader)
    altered = _dataset()
    assert [item["query_id"] for item in altered["ledger"]] == [
        item["query_id"] for item in baseline["ledger"]
    ]
    assert [case["query_id"] for case in altered["cases"]] == [
        case["query_id"] for case in baseline["cases"]
    ]


def test_ambiguous_human_source_is_retained_as_pending_issue(monkeypatch):
    original = answer_path_data._evidence_range

    def ambiguous(*args, **kwargs):
        return None if args[0].startswith("The ") else original(*args, **kwargs)

    monkeypatch.setattr(answer_path_data, "_evidence_range", ambiguous)
    dataset = _dataset()
    assert len(dataset["cases"]) == 18
    assert any("HUMAN_EVIDENCE_UNALIGNED" in case["curation"]["notes"] for case in dataset["cases"])


def test_ineligible_proposals_remain_in_full_ledger():
    dataset = _dataset()
    assert len(dataset["ledger"]) == 18
    assert len(dataset["cases"]) == 18
    assert all(item["selected"] for item in dataset["ledger"])
    assert any(not item["eligible"] for item in dataset["ledger"])
    assert dataset["selection"]["eligible_count"] == 17


def _render(paper_id):
    return answer_path_data.article_and_spans(_paper(paper_id))[0]


def _paper(paper_id):
    frozen = answer_path_data._load_object(
        Path("benchmarks/public_rag/results/paragraph-qa-20261023/frozen-dataset.json")
    )
    return next(item for item in frozen["corpus"] if item["paper_id"] == paper_id)
