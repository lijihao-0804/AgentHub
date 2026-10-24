from benchmarks.public_rag.root_cause_dataset import paper_ids, source_audit, source_keys


def test_exposure_scan_finds_nested_real_ids_without_treating_scores_as_sources():
    train = {"p1", "p2", "p3", "p5", "p6"}
    value = {
        "rows": [{"paper_id": "p1"}, {"paper_id": "unknown"}],
        "sampling": {"selected_paper_ids": ["p2"]},
        "p3": {"score": 1},
        "score": "p4",
        "query_id": "p4",
        "nested_run": {"group_id": "p5"},
        "meta": {"group_ids": ["p6"]},
    }
    assert paper_ids(value, train) == train


def test_source_overlap_check_uses_question_and_source_content_not_answer_labels():
    class NoAnswers(dict):
        def __getitem__(self, key):
            if key == "answers":
                raise AssertionError("gold labels must not affect sampling")
            return super().__getitem__(key)

    paragraph = "A source paragraph used for overlap screening. " * 6
    paper = {
        "title": "A title",
        "qas": [NoAnswers(question="Which data?")],
        "full_text": [{"paragraphs": [paragraph]}],
    }
    keys = source_keys(paper)
    assert ("paragraph", " ".join(paragraph.casefold().split())) in keys
    assert ("question", "which data?") in keys


def test_conflicting_yes_no_gold_cannot_be_certified_by_source_alignment(monkeypatch):
    from benchmarks.public_rag import paragraph_qa_live

    monkeypatch.setattr(paragraph_qa_live, "article_and_spans", lambda paper: ("Visible.", []))
    data = {
        "corpus": [{"paper_id": "p"}],
        "cases": [
            {
                "query_id": "q",
                "paper_id": "p",
                "question": "Was it compared?",
                "consensus": "answerable",
                "annotations": [
                    {"answer_type": "yes_no", "answer": "Yes", "evidence": ["Visible."]},
                    {"answer_type": "yes_no", "answer": "No", "evidence": []},
                ],
            }
        ],
    }
    result = source_audit(data)
    assert result["registered"] == 1
    assert result["issue_counts"]["CONTRADICTORY_YES_NO_ANNOTATIONS"] == 1
    assert result["cases"][0]["semantic_sufficiency"] == "UNKNOWN"
