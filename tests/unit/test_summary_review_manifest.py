"""Protected summary source review binds exact facts without authorizing dispatch."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from uuid import UUID

import pytest

from packages.agent_runtime.summary_prompt import (
    SummaryInputContext,
    SummaryPromptSource,
    SummaryProtectedFact,
    build_summary_prompt,
)
from packages.agent_runtime.summary_source import stable_summary_source_identity
from packages.agent_runtime.work_layer import (
    HISTORY_SOURCE_COUNT_VERSION,
    ThreadHistorySourceIdentity,
    ThreadHistorySourceManifest,
    ThreadHistorySourceScope,
    ThreadTurnContext,
)
from packages.model_gateway.contracts import ModelMessage
from packages.threads.summary_review_manifest import (
    MAX_SUMMARY_PROTECTED_EXCERPTS_PER_SOURCE,
    MAX_SUMMARY_PROTECTED_SOURCES,
    MAX_SUMMARY_REVIEW_IDENTIFIER_LENGTH,
    SUMMARY_REVIEW_ASSERTIONS,
    SummaryExcerptRole,
    SummaryProtectedExcerptRequest,
    SummaryReviewDecision,
    SummaryReviewManifestError,
    SummarySourceReviewAssertion,
    build_summary_review_manifest,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
THREAD_ID = UUID("00000000-0000-0000-0000-000000000002")
CURRENT_RUN_ID = UUID("00000000-0000-0000-0000-000000000003")
LATER_RUN_ID = UUID("00000000-0000-0000-0000-000000000004")
REVIEWER_ID = UUID("00000000-0000-0000-0000-000000000005")
REVIEW_ID = UUID("00000000-0000-0000-0000-000000000006")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _turn_and_source(
    sequence: int = 1,
    *,
    user_input: str = "请保留“100 CNY”。",
    final_output: str = "已记录，金额为「100 CNY」！",
) -> tuple[ThreadTurnContext, ThreadHistorySourceIdentity]:
    turn_id = UUID(int=10_000 + sequence)
    run_id = UUID(int=20_000 + sequence)
    source_id = f"thread-turn:{turn_id}"
    turn = ThreadTurnContext(
        user_input=user_input,
        final_output=final_output,
        turn_id=turn_id,
        run_id=run_id,
        sequence=sequence,
        messages=(
            ModelMessage(role="user", content=user_input),
            ModelMessage(role="assistant", content=final_output),
        ),
    )
    source = ThreadHistorySourceIdentity(
        source_id=source_id,
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        turn_id=turn_id,
        run_id=run_id,
        sequence=sequence,
        watermark=sequence + 1,
        exchange_id=source_id,
        original_ref=source_id,
        count_version=HISTORY_SOURCE_COUNT_VERSION,
        user_input_sha256=_sha256(user_input),
        final_output_sha256=_sha256(final_output),
        revision_sha256=_sha256(source_id),
        summary_eligibility="UNKNOWN",
        summary_eligibility_reason="PLANNER_SEMANTIC_ELIGIBILITY_NOT_CLASSIFIED",
    )
    return turn, source


def _manifest(
    sources: tuple[ThreadHistorySourceIdentity, ...],
    *,
    before_sequence: int | None = None,
    before_run_id: UUID = CURRENT_RUN_ID,
) -> ThreadHistorySourceManifest:
    highest_sequence = max((source.sequence for source in sources), default=0)
    watermark = before_sequence or highest_sequence + 1
    selected_sources = tuple(replace(source, watermark=watermark) for source in sources)
    max_turns = max(1, len(selected_sources))
    scope = ThreadHistorySourceScope(
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        before_run_id=before_run_id,
        before_sequence=watermark,
        max_turns=max_turns,
        max_turns_scanned=max_turns,
        max_artifacts=1,
        artifact_page_size=1,
    )
    return ThreadHistorySourceManifest(
        scope=scope,
        status="COMPLETE",
        selected_sources=selected_sources,
        selected_source_count=len(selected_sources),
        available_eligible_source_count=len(selected_sources),
        selected_artifact_count=0,
        available_artifact_count=0,
        available_artifact_count_scope="FROZEN_WATERMARK",
        available_source_count=len(selected_sources),
        available_source_count_scope="FROZEN_WATERMARK",
        provenance_complete=True,
        below_watermark_has_eligible_source=False,
        work_limit_reached=False,
    )


def _review(
    source_identity: str,
    *,
    decision: SummaryReviewDecision | str = SummaryReviewDecision.ELIGIBLE,
    admin: bool = True,
    revision: int = 1,
) -> SummarySourceReviewAssertion:
    return SummarySourceReviewAssertion(
        source_identity=source_identity,
        decision=decision,
        reviewed_by=REVIEWER_ID,
        revision=revision,
        reviewer_is_workspace_admin=admin,
        review_id=REVIEW_ID,
    )


@dataclass(frozen=True)
class _Fixture:
    turn: ThreadTurnContext
    source: ThreadHistorySourceIdentity
    manifest: ThreadHistorySourceManifest
    identity: str
    review: SummarySourceReviewAssertion


def _fixture(
    *,
    sequence: int = 1,
    user_input: str = "请保留“100 CNY”。",
    final_output: str = "已记录，金额为「100 CNY」！",
) -> _Fixture:
    turn, original_source = _turn_and_source(
        sequence,
        user_input=user_input,
        final_output=final_output,
    )
    manifest = _manifest((original_source,))
    source = manifest.selected_sources[0]
    identity = stable_summary_source_identity(source, turn)
    return _Fixture(turn, source, manifest, identity, _review(identity))


def _build(
    fixture: _Fixture,
    excerpts: tuple[SummaryProtectedExcerptRequest, ...] = (),
    **overrides: object,
):
    arguments: dict[str, object] = {
        "manifest": fixture.manifest,
        "materialized_turns": (fixture.turn,),
        "source_reviews": (fixture.review,),
        "excerpts": excerpts,
        "workspace_id": WORKSPACE_ID,
        "thread_id": THREAD_ID,
        "before_run_id": CURRENT_RUN_ID,
        "before_sequence": fixture.manifest.scope.before_sequence,
        "protected_manifest_complete": True,
    }
    arguments.update(overrides)
    return build_summary_review_manifest(**arguments)


def test_unknown_frozen_eligibility_uses_independent_eligible_review():
    fixture = _fixture()
    fact = SummaryProtectedExcerptRequest(
        fact_ref="amount-and-non-execution",
        source_id=fixture.source.source_id,
        role=SummaryExcerptRole.USER,
        start_offset=0,
        end_offset=len(fixture.turn.user_input),
    )

    result = _build(fixture, (fact,))

    assert fixture.source.summary_eligibility == "UNKNOWN"
    assert result.audit.sources[0].frozen_summary_eligibility == "UNKNOWN"
    assert result.audit.sources[0].review_decision is SummaryReviewDecision.ELIGIBLE
    assert result.audit.sources[0].reviewed_by == REVIEWER_ID
    assert result.audit.sources[0].review_revision == 1
    assert result.audit.sources[0].reviewer_is_workspace_admin is True
    assert result.audit.server_revalidation_required is True
    assert result.audit.assertions == SUMMARY_REVIEW_ASSERTIONS
    assert result.for_prompt() == result.protected_facts
    assert isinstance(result.protected_facts[0], SummaryProtectedFact)
    assert result.protected_facts[0].text == fixture.turn.user_input
    assert result.protected_facts[0].source_ids == (fixture.source.source_id,)


def test_explicitly_ineligible_frozen_source_cannot_be_overridden_by_a_review():
    fixture = _fixture()
    ineligible = replace(fixture.source, summary_eligibility="INELIGIBLE")
    manifest = _manifest((ineligible,))
    with pytest.raises(SummaryReviewManifestError, match="frozen as ineligible"):
        _build(fixture, manifest=manifest)


def test_extra_unreviewable_transcript_messages_remain_exact():
    fixture = _fixture()
    changed_turn = replace(
        fixture.turn,
        messages=(
            ModelMessage("user", fixture.turn.user_input),
            ModelMessage("assistant", "additional unreviewed context"),
            ModelMessage("assistant", fixture.turn.final_output),
        ),
    )
    identity = stable_summary_source_identity(fixture.source, changed_turn)
    with pytest.raises(SummaryReviewManifestError, match="unsupported transcript"):
        _build(
            fixture,
            materialized_turns=(changed_turn,),
            source_reviews=(_review(identity),),
        )


def test_unbounded_turn_and_excerpt_lists_are_rejected_before_processing():
    fixture = _fixture()
    with pytest.raises(SummaryReviewManifestError, match="materialized_turns exceeds"):
        _build(fixture, materialized_turns=(fixture.turn,) * (MAX_SUMMARY_PROTECTED_SOURCES + 1))
    excerpt = SummaryProtectedExcerptRequest(
        fact_ref="amount", source_id=fixture.source.source_id, role="user", start_offset=0,
        end_offset=1,
    )
    with pytest.raises(SummaryReviewManifestError, match="excerpts exceeds"):
        _build(
            fixture,
            excerpts=(excerpt,)
            * (MAX_SUMMARY_PROTECTED_SOURCES * MAX_SUMMARY_PROTECTED_EXCERPTS_PER_SOURCE + 1),
        )


def test_before_run_and_watermark_must_exactly_match_manifest():
    fixture = _fixture()
    with pytest.raises(SummaryReviewManifestError, match="manifest scope"):
        _build(fixture, before_run_id=LATER_RUN_ID)
    with pytest.raises(SummaryReviewManifestError, match="manifest scope"):
        _build(fixture, before_sequence=fixture.manifest.scope.before_sequence + 1)


def test_same_verified_source_has_same_identity_at_a_later_watermark():
    turn, original_source = _turn_and_source()
    first_manifest = _manifest((original_source,), before_sequence=3)
    later_manifest = _manifest(
        (original_source,),
        before_sequence=7,
        before_run_id=LATER_RUN_ID,
    )
    first_source = first_manifest.selected_sources[0]
    later_source = later_manifest.selected_sources[0]
    first_identity = stable_summary_source_identity(first_source, turn)
    later_identity = stable_summary_source_identity(later_source, turn)
    excerpt = SummaryProtectedExcerptRequest(
        fact_ref="stable-fact",
        source_id=first_source.source_id,
        role=SummaryExcerptRole.USER,
        start_offset=0,
        end_offset=len(turn.user_input),
    )

    first = build_summary_review_manifest(
        manifest=first_manifest,
        materialized_turns=(turn,),
        source_reviews=(_review(first_identity),),
        excerpts=(excerpt,),
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        before_run_id=CURRENT_RUN_ID,
        before_sequence=3,
        protected_manifest_complete=True,
    )
    later = build_summary_review_manifest(
        manifest=later_manifest,
        materialized_turns=(turn,),
        source_reviews=(_review(later_identity),),
        excerpts=(excerpt,),
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        before_run_id=LATER_RUN_ID,
        before_sequence=7,
        protected_manifest_complete=True,
    )

    assert first_identity == later_identity
    assert first.audit.sources[0].source_identity == later.audit.sources[0].source_identity
    assert first.protected_facts == later.protected_facts


def test_changed_materialized_source_fails_stable_identity_binding():
    fixture = _fixture()
    changed_turn = replace(
        fixture.turn,
        user_input="changed after review",
        messages=(
            ModelMessage("user", "changed after review"),
            ModelMessage("assistant", fixture.turn.final_output),
        ),
    )

    with pytest.raises(SummaryReviewManifestError, match="no longer matches"):
        _build(fixture, materialized_turns=(changed_turn,))


@pytest.mark.parametrize(
    ("change", "message"),
    (
        (
            "artifact",
            "artifacts",
        ),
        (
            "tool",
            "unsupported role",
        ),
        (
            "evidence",
            "evidence registry",
        ),
        (
            "knowledge",
            "knowledge snapshots",
        ),
        (
            "wiki",
            "wiki publications",
        ),
    ),
)
def test_tools_artifacts_and_source_evidence_fail_closed(change: str, message: str):
    fixture = _fixture()
    turn = fixture.turn
    if change == "artifact":
        turn = replace(
            turn,
            artifact_ids=(UUID(int=90_001),),
            artifact_refs=("artifact://retained",),
        )
    elif change == "tool":
        turn = replace(
            turn,
            messages=(
                *turn.messages,
                ModelMessage(role="tool", content="tool output", tool_call_id="call-1"),
            ),
        )
    elif change == "evidence":
        turn = replace(turn, evidence_registry={"documents": ["chunk-1"]})
    elif change == "knowledge":
        turn = replace(turn, source_knowledge_snapshots=({"snapshot": "1"},))
    else:
        turn = replace(turn, source_wiki_publications=({"publication": "1"},))

    with pytest.raises(SummaryReviewManifestError, match=message):
        _build(fixture, materialized_turns=(turn,))


def test_exact_unicode_offsets_reconstruct_chinese_punctuation_and_assistant_text():
    user_input = "前文「100 CNY，不得执行！」以及后文。"
    final_output = "结论：金额“100 CNY”保持不变；不要自动退款。"
    fixture = _fixture(user_input=user_input, final_output=final_output)
    user_start = user_input.index("「")
    user_end = user_input.index("」") + 1
    assistant_start = final_output.index("“")
    assistant_end = final_output.index("”") + 1
    requests = (
        SummaryProtectedExcerptRequest(
            fact_ref="user-exact",
            source_id=fixture.source.source_id,
            role="user",
            start_offset=user_start,
            end_offset=user_end,
        ),
        SummaryProtectedExcerptRequest(
            fact_ref="assistant-exact",
            source_id=fixture.source.source_id,
            role="assistant",
            start_offset=assistant_start,
            end_offset=assistant_end,
        ),
    )

    result = _build(fixture, requests)

    assert [fact.text for fact in result.protected_facts] == [
        "「100 CNY，不得执行！」",
        "“100 CNY”",
    ]
    assert result.audit.excerpts[0].start_offset == user_start
    assert result.audit.excerpts[0].end_offset == user_end
    assert result.audit.excerpts[1].role is SummaryExcerptRole.ASSISTANT


@pytest.mark.parametrize(
    ("start", "end", "message"),
    (
        (0, 999, "outside source"),
        (5, 6, "boundaries"),
    ),
)
def test_invalid_or_whitespace_boundaries_are_rejected(start: int, end: int, message: str):
    fixture = _fixture(user_input="alpha beta")
    request = SummaryProtectedExcerptRequest(
        fact_ref="invalid-range",
        source_id=fixture.source.source_id,
        role="user",
        start_offset=start,
        end_offset=end,
    )
    with pytest.raises(SummaryReviewManifestError, match=message):
        _build(fixture, (request,))


@pytest.mark.parametrize(
    ("start", "end", "message"),
    (
        (0, 0, "non-empty"),
        (0, -1, "non-negative"),
        (True, 2, "non-negative"),
    ),
)
def test_empty_negative_and_boolean_offsets_are_rejected(start: int, end: int, message: str):
    with pytest.raises(SummaryReviewManifestError, match=message):
        SummaryProtectedExcerptRequest(
            fact_ref="invalid-offset",
            source_id="thread-turn:source",
            role="user",
            start_offset=start,
            end_offset=end,
        )


def test_duplicate_fact_ref_and_duplicate_coordinate_are_rejected():
    fixture = _fixture(user_input="abcdef")
    first = SummaryProtectedExcerptRequest("same-ref", fixture.source.source_id, "user", 0, 1)
    second = SummaryProtectedExcerptRequest("same-ref", fixture.source.source_id, "user", 1, 2)
    with pytest.raises(SummaryReviewManifestError, match="duplicate protected fact_ref"):
        _build(fixture, (first, second))

    third = SummaryProtectedExcerptRequest("other-ref", fixture.source.source_id, "user", 0, 1)
    with pytest.raises(SummaryReviewManifestError, match="duplicate protected excerpt coordinate"):
        _build(fixture, (first, third))


def test_excerpts_per_source_cap_is_100():
    text = "a" * (MAX_SUMMARY_PROTECTED_EXCERPTS_PER_SOURCE + 1)
    fixture = _fixture(user_input=text)
    accepted = tuple(
        SummaryProtectedExcerptRequest(
            f"fact-{offset}",
            fixture.source.source_id,
            "user",
            offset,
            offset + 1,
        )
        for offset in range(MAX_SUMMARY_PROTECTED_EXCERPTS_PER_SOURCE)
    )
    assert len(_build(fixture, accepted).protected_facts) == 100

    overflow = (
        *accepted,
        SummaryProtectedExcerptRequest(
            "fact-overflow",
            fixture.source.source_id,
            "user",
            100,
            101,
        ),
    )
    with pytest.raises(SummaryReviewManifestError, match="exceeds 100"):
        _build(fixture, overflow)


def test_identifier_length_is_bounded():
    with pytest.raises(SummaryReviewManifestError, match="at most 256"):
        SummaryProtectedExcerptRequest(
            "x" * (MAX_SUMMARY_REVIEW_IDENTIFIER_LENGTH + 1),
            "source",
            "user",
            0,
            1,
        )


def test_admin_eligibility_and_protected_manifest_completeness_are_required_assertions():
    fixture = _fixture()
    with pytest.raises(SummaryReviewManifestError, match="workspace-admin"):
        non_admin = replace(fixture.review, reviewer_is_workspace_admin=False)
        _build(fixture, source_reviews=(non_admin,))
    with pytest.raises(SummaryReviewManifestError, match="ELIGIBLE review"):
        _build(fixture, source_reviews=(replace(fixture.review, decision="INELIGIBLE"),))
    with pytest.raises(SummaryReviewManifestError, match="explicit true caller assertion"):
        _build(fixture, protected_manifest_complete=False)


def test_reviews_and_materialized_turns_must_exactly_match_selected_membership():
    fixture = _fixture()
    missing_reviews_message = "exactly match selected stable identities"
    with pytest.raises(SummaryReviewManifestError, match=missing_reviews_message):
        _build(fixture, source_reviews=())

    extra_turn, _ = _turn_and_source(2)
    with pytest.raises(SummaryReviewManifestError, match="exactly match"):
        _build(fixture, materialized_turns=(fixture.turn, extra_turn))


def test_200_selected_sources_are_supported():
    fixtures = [
        _turn_and_source(sequence) for sequence in range(1, MAX_SUMMARY_PROTECTED_SOURCES + 1)
    ]
    turns = tuple(turn for turn, _ in fixtures)
    sources = tuple(source for _, source in fixtures)
    manifest = _manifest(sources, before_sequence=MAX_SUMMARY_PROTECTED_SOURCES + 1)
    identities = tuple(
        stable_summary_source_identity(source, turn)
        for source, turn in zip(manifest.selected_sources, turns, strict=True)
    )

    result = build_summary_review_manifest(
        manifest=manifest,
        materialized_turns=turns,
        source_reviews=tuple(_review(identity) for identity in identities),
        excerpts=(),
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        before_run_id=CURRENT_RUN_ID,
        before_sequence=MAX_SUMMARY_PROTECTED_SOURCES + 1,
        protected_manifest_complete=True,
    )

    assert len(result.audit.sources) == MAX_SUMMARY_PROTECTED_SOURCES
    assert result.protected_facts == ()


def test_reviewer_and_audit_metadata_never_enter_summary_prompt():
    first = _fixture()
    second_turn, second_source = _turn_and_source(
        2,
        user_input="第二问：状态未知，不要重试。",
        final_output="第二答：保持 UNKNOWN_OUTCOME。",
    )
    manifest = _manifest((first.source, second_source))
    first_source, second_source = manifest.selected_sources
    first_identity = stable_summary_source_identity(first_source, first.turn)
    second_identity = stable_summary_source_identity(second_source, second_turn)
    result = build_summary_review_manifest(
        manifest=manifest,
        materialized_turns=(first.turn, second_turn),
        source_reviews=(_review(first_identity), _review(second_identity, revision=2)),
        excerpts=(
            SummaryProtectedExcerptRequest(
                "first-fact",
                first_source.source_id,
                "user",
                0,
                len(first.turn.user_input),
            ),
            SummaryProtectedExcerptRequest(
                "second-fact",
                second_source.source_id,
                "assistant",
                0,
                len(second_turn.final_output),
            ),
        ),
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        before_run_id=CURRENT_RUN_ID,
        before_sequence=3,
        protected_manifest_complete=True,
    )
    prompt_sources = (
        SummaryPromptSource(
            source_id=first_source.source_id,
            workspace_id=str(WORKSPACE_ID),
            thread_id=str(THREAD_ID),
            identity=first_identity,
            revision=first_source.revision_sha256,
            sequence=1,
            watermark=3,
            role="user",
            speaker="Alice",
            occurred_at="2026-10-28T09:00:00+08:00",
            content=first.turn.user_input,
            original_ref=first_source.original_ref,
            summary_eligible=True,
            exchange_complete=True,
        ),
        SummaryPromptSource(
            source_id=second_source.source_id,
            workspace_id=str(WORKSPACE_ID),
            thread_id=str(THREAD_ID),
            identity=second_identity,
            revision=second_source.revision_sha256,
            sequence=2,
            watermark=3,
            role="assistant",
            speaker="AgentHub",
            occurred_at="2026-10-28T09:01:00+08:00",
            content=second_turn.final_output,
            original_ref=second_source.original_ref,
            summary_eligible=True,
            exchange_complete=True,
        ),
    )
    context = SummaryInputContext(
        workspace_id=str(WORKSPACE_ID),
        thread_id=str(THREAD_ID),
        permission_snapshot="permission-snapshot",
        task_goal="继续当前任务。",
        prefix_first_sequence=1,
        prefix_last_sequence=2,
        watermark=3,
        sources=prompt_sources,
        protected_facts=result.for_prompt(),
        completed_actions=(),
        required_references=(),
        source_budget_tokens=1500,
        target_budget_tokens=1000,
        final_view_budget_tokens=3000,
        prefix_complete=True,
        protected_manifest_complete=True,
        action_manifest_complete=True,
        reference_manifest_complete=True,
    )

    prompt = build_summary_prompt(context)
    prompt_text = prompt.system_prompt + "\n" + prompt.user_prompt
    audit_record = result.audit.to_record()

    assert str(REVIEWER_ID) in str(audit_record)
    assert str(REVIEW_ID) in str(audit_record)
    for forbidden in (
        str(REVIEWER_ID),
        str(REVIEW_ID),
        "reviewed_by",
        "review_revision",
        "reviewer_is_workspace_admin",
        "server_revalidation_required",
        "summary-protected-review-v1",
        "start_offset",
    ):
        assert forbidden not in prompt_text
    assert result.protected_facts[0].text in prompt_text
    assert result.protected_facts[1].text in prompt_text
