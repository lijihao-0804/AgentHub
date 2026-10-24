import pytest

from benchmarks.public_rag.paragraph_qa_run_gate import validate_execution_name


def test_new_directory_does_not_authorize_replaying_paid_questions():
    with pytest.raises(ValueError, match="registered initial"):
        validate_execution_name("live-run-3", None, None)
    validate_execution_name("live-run-1", None, None)


def test_amendment_requires_exact_remainder_and_prior_execution():
    amendment = {"original_run": "live-run-1", "new_run": "live-run-2"}
    for name, prior in (("live-run-3", None), ("live-run-1", None), ("live-run-2", None)):
        with pytest.raises(ValueError, match="registered untouched remainder"):
            validate_execution_name(name, prior, amendment)
    validate_execution_name("live-run-2", "live-run-1", amendment)
