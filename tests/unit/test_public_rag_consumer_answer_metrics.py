import pytest

from benchmarks.public_rag.consumer_answer_metrics import answer_metrics, citation_metrics


def test_answer_metrics_normalizes_and_uses_best_alias() -> None:
    result = answer_metrics(
        "The  CAT!",
        ["a cat", "unrelated answer", "An CAT."],
    )

    assert result == {"exact_match": 1, "token_f1": 1.0}


def test_answer_metrics_counts_duplicate_tokens_in_f1() -> None:
    result = answer_metrics("red red blue", ["red blue blue"])

    # Counter intersection is one red and one blue: P=R=2/3.
    assert result["exact_match"] == 0
    assert result["token_f1"] == pytest.approx(2 / 3)


def test_answer_metrics_returns_zero_for_missing_or_normatively_empty_prediction() -> None:
    assert answer_metrics("", ["a cat"]) == {"exact_match": 0, "token_f1": 0.0}
    assert answer_metrics("the", ["cat"]) == {"exact_match": 0, "token_f1": 0.0}
    assert answer_metrics("!!!", ["cat"]) == {"exact_match": 0, "token_f1": 0.0}


def test_answer_metrics_does_not_credit_two_normatively_empty_strings() -> None:
    assert answer_metrics("the!", ["a"]) == {"exact_match": 0, "token_f1": 0.0}
    assert answer_metrics("cat", ["!!!"]) == {"exact_match": 0, "token_f1": 0.0}


@pytest.mark.parametrize(
    ("prediction", "references", "exception", "message"),
    [
        (None, ["cat"], TypeError, "prediction"),
        ("cat", None, TypeError, "references"),
        ("cat", [], ValueError, "at least one"),
        ("cat", ["cat", ""], ValueError, "empty"),
        ("cat", ["cat", 3], TypeError, "reference"),
        ("cat", "cat", TypeError, "references"),
    ],
)
def test_answer_metrics_rejects_invalid_inputs(
    prediction: object,
    references: object,
    exception: type[Exception],
    message: str,
) -> None:
    with pytest.raises(exception, match=message):
        answer_metrics(prediction, references)  # type: ignore[arg-type]


def test_citation_metrics_counts_unknown_and_duplicate_ids() -> None:
    result = citation_metrics(
        ["chunk-a", "chunk-a", "chunk-b", "chunk-missing"],
        ["chunk-a", "chunk-b", "chunk-unused"],
    )

    assert result == {
        "validity": 2 / 3,
        "unknown": 1,
        "duplicates": 1,
        "has_citations": True,
    }


def test_citation_metrics_does_not_turn_no_citations_into_faithfulness() -> None:
    assert citation_metrics([], ["chunk-a"]) == {
        "validity": 0.0,
        "unknown": 0,
        "duplicates": 0,
        "has_citations": False,
    }


@pytest.mark.parametrize(
    ("cited", "admitted", "exception", "message"),
    [
        (None, [], TypeError, "cited_chunk_ids"),
        ([], None, TypeError, "admitted_chunk_ids"),
        (["chunk-a"], ("chunk-a",), TypeError, "admitted_chunk_ids"),
        (["chunk-a", 1], [], ValueError, "cited_chunk_ids"),
        ([""], [], ValueError, "cited_chunk_ids"),
        ([], [""], ValueError, "admitted_chunk_ids"),
    ],
)
def test_citation_metrics_rejects_non_string_or_non_list_inputs(
    cited: object,
    admitted: object,
    exception: type[Exception],
    message: str,
) -> None:
    with pytest.raises(exception, match=message):
        citation_metrics(cited, admitted)  # type: ignore[arg-type]
