from benchmarks.public_rag.reader_value_audit import polarity_view


def result(answer):
    return {"status": "VALID", "answer": answer, "unanswerable": False}


def test_value_match_format_and_source_support_are_independent():
    verbose = polarity_view(result("Yes — the paper reports an ELMo comparison."), ["Yes"])
    assert verbose["reference_match"] is True and verbose["bare_value_format"] is False
    assert verbose["semantic_support"] == "UNKNOWN"
    incorrect = polarity_view(result("No"), ["Yes"])
    assert incorrect["reference_match"] is False and incorrect["bare_value_format"] is True


def test_disputed_labels_unknown_outputs_and_wrong_answer_types_never_receive_value_credit():
    assert polarity_view(result("Yes"), ["Yes", "No"])["reference_match"] is None
    assert polarity_view({"status": "UNKNOWN_OUTCOME"}, ["Yes"])["reference_match"] is None
    assert (
        polarity_view(result("No"), ["dataset A"])["status"] == "NOT_A_REGISTERED_POLAR_REFERENCE"
    )
