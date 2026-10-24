import pytest

from benchmarks.public_rag.evidence_budget_diagnostic import diagnose


def test_raw_budget_precedes_document_deduplication():
    baseline = (
        [{"document_id": "A"}] * 5
        + [{"document_id": "B"}]
        + [{"document_id": f"C{i}"} for i in range(14)]
    )
    candidate = [baseline[5], *baseline[:5], *baseline[6:]]
    row = {"query_id": "q", "baseline_ranking": baseline, "final_ranking": candidate}
    result = diagnose([row], {"q": {"B": 1}})["raw_chunk_budgets"]
    assert result[5]["baseline"]["macro_source_recall_in_exposure"] == 0
    assert result[5]["candidate"]["macro_source_recall_in_exposure"] == 1
    assert result[6]["baseline"]["macro_source_recall_in_exposure"] == 1
    assert result[5]["baseline"]["mean_unique_sources"] == 1
    with pytest.raises(ValueError):
        diagnose([], {"q": {"B": 1}})
