"""Summary dispatch crash windows must not cause a silent second call."""

import pytest

from packages.agent_runtime.summary_state import (
    SummaryJobStatus,
    SummaryState,
    SummaryTransitionError,
    claim,
    claim_recovery,
    commit_dispatch_intent,
    fail_before_dispatch,
    publish,
    recover_expired_lease,
    store_response,
    validate_response,
)


def test_claim_expiry_before_intent_can_be_retried_without_reservation():
    first = claim(SummaryState(), lease_token="worker-a")
    recovered = recover_expired_lease(first)
    assert recovered == SummaryState()
    assert claim(recovered, lease_token="worker-b").status is SummaryJobStatus.CLAIMED


def test_expiry_after_intent_is_unknown_and_keeps_reservation():
    first = commit_dispatch_intent(
        claim(SummaryState(), lease_token="worker-a"), lease_token="worker-a"
    )
    recovered = recover_expired_lease(first)
    assert recovered.status is SummaryJobStatus.UNKNOWN_OUTCOME
    assert recovered.budget_reserved
    with pytest.raises(SummaryTransitionError):
        claim(recovered, lease_token="worker-b")
    with pytest.raises(SummaryTransitionError):
        store_response(recovered, lease_token="worker-a", response_ref="late")
    with pytest.raises(SummaryTransitionError):
        fail_before_dispatch(first, lease_token="worker-a")


def test_local_rejection_before_dispatch_is_definite_and_has_no_reservation():
    rejected = fail_before_dispatch(claim(SummaryState(), lease_token="owner"), lease_token="owner")
    assert rejected.status is SummaryJobStatus.FAILED
    assert not rejected.budget_reserved


def test_saved_response_recovers_without_another_provider_dispatch():
    claimed = claim(SummaryState(), lease_token="old")
    dispatched = commit_dispatch_intent(claimed, lease_token="old")
    saved = store_response(dispatched, lease_token="old", response_ref="response-1")
    recovered = claim_recovery(recover_expired_lease(saved), lease_token="new")
    with pytest.raises(SummaryTransitionError):
        commit_dispatch_intent(recovered, lease_token="new")
    valid = validate_response(recovered, lease_token="new", valid=True)
    published = publish(valid, lease_token="new", revision_ref="revision-1", bindings_current=True)
    assert published.status is SummaryJobStatus.PUBLISHED
    assert published.response_ref == "response-1" and published.revision_ref == "revision-1"


def test_expired_worker_cannot_publish_and_changed_binding_is_stale():
    claimed = claim(SummaryState(), lease_token="old")
    sent = commit_dispatch_intent(claimed, lease_token="old")
    saved = store_response(sent, lease_token="old", response_ref="response-1")
    validated = validate_response(saved, lease_token="old", valid=True)
    recovered = claim_recovery(recover_expired_lease(validated), lease_token="new")
    with pytest.raises(SummaryTransitionError):
        publish(recovered, lease_token="old", revision_ref="revision-1", bindings_current=True)
    stale = publish(recovered, lease_token="new", revision_ref="revision-1", bindings_current=False)
    assert stale.status is SummaryJobStatus.STALE
    assert stale.response_ref == "response-1" and stale.budget_reserved


def test_failed_validation_cannot_publish_and_keeps_response():
    state = claim(SummaryState(), lease_token="owner")
    state = commit_dispatch_intent(state, lease_token="owner")
    state = store_response(state, lease_token="owner", response_ref="bad-response")
    state = validate_response(state, lease_token="owner", valid=False)
    assert state.status is SummaryJobStatus.FAILED and state.response_ref == "bad-response"
    with pytest.raises(SummaryTransitionError):
        publish(state, lease_token="owner", revision_ref="bad", bindings_current=True)


def test_string_flags_and_nonstring_lease_cannot_bypass_validation():
    with pytest.raises(SummaryTransitionError):
        claim(SummaryState(), lease_token=1)
    state = claim(SummaryState(), lease_token="owner")
    state = commit_dispatch_intent(state, lease_token="owner")
    state = store_response(state, lease_token="owner", response_ref="stored")
    with pytest.raises(SummaryTransitionError):
        validate_response(state, lease_token="owner", valid="false")
    state = validate_response(state, lease_token="owner", valid=True)
    with pytest.raises(SummaryTransitionError):
        publish(state, lease_token="owner", revision_ref="revision", bindings_current="false")
