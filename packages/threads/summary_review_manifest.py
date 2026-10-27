"""Pure protected-fact review manifest for Thread summaries.

This module binds a caller-supplied review assertion to a verified, complete M3
source manifest and to exact excerpts reconstructed from its materialized Turns.
It does not authorize a model request, inspect the database, or prove that an
admin identified every protected fact. A server must revalidate the reviewer's
current permission, the latest review revision, source integrity, and manifest
completeness before using the resulting facts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from packages.agent_runtime.summary_prompt import (
    SummaryInputContextError,
    SummaryProtectedFact,
)
from packages.agent_runtime.summary_source import stable_summary_source_identity
from packages.agent_runtime.work_layer import (
    HISTORY_SOURCE_COMPLETE,
    ThreadHistorySourceIdentity,
    ThreadHistorySourceManifest,
    ThreadTurnContext,
)
from packages.model_gateway.contracts import ModelMessage

SUMMARY_REVIEW_MANIFEST_VERSION: Final = "summary-protected-review-v1"
MAX_SUMMARY_PROTECTED_SOURCES: Final = 200
MAX_SUMMARY_PROTECTED_EXCERPTS_PER_SOURCE: Final = 100
MAX_SUMMARY_REVIEW_IDENTIFIER_LENGTH: Final = 256
MAX_SUMMARY_REVIEW_REVISION: Final = 2_147_483_647

SUMMARY_REVIEW_ASSERTIONS: Final = (
    "The admin author, workspace-admin status, eligibility decision, and review "
    "revision are caller assertions; this builder does not revalidate them.",
    "Explicit protected-manifest completeness is a caller assertion; this builder "
    "does not prove that every protected fact was identified.",
    "The real server must revalidate current admin permission, latest review "
    "revision, frozen source integrity, and protected-manifest completeness.",
)


class SummaryReviewManifestError(ValueError):
    """The protected review input is invalid or fails closed."""


class SummaryReviewDecision(StrEnum):
    """The independently reviewed eligibility decision."""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNKNOWN = "UNKNOWN"


class SummaryExcerptRole(StrEnum):
    """Source roles that may supply an exact protected excerpt."""

    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class SummarySourceReviewAssertion:
    """One server-supplied review assertion bound to a stable source identity.

    The fields are assertions from a trusted caller. This value does not prove
    that ``reviewed_by`` is currently an admin or that ``revision`` is latest.
    """

    source_identity: str
    decision: SummaryReviewDecision | str
    reviewed_by: UUID
    revision: int
    reviewer_is_workspace_admin: bool
    review_id: UUID | None = None

    def __post_init__(self) -> None:
        _require_sha256(self.source_identity, "source review identity")
        try:
            decision = SummaryReviewDecision(self.decision)
        except (TypeError, ValueError):
            raise SummaryReviewManifestError("source review decision is invalid") from None
        object.__setattr__(self, "decision", decision)
        if not isinstance(self.reviewed_by, UUID):
            raise SummaryReviewManifestError("reviewed_by must be a UUID")
        _require_positive_bounded_int(
            self.revision, "review revision", maximum=MAX_SUMMARY_REVIEW_REVISION
        )
        if not isinstance(self.reviewer_is_workspace_admin, bool):
            raise SummaryReviewManifestError(
                "reviewer_is_workspace_admin must be an explicit bool assertion"
            )
        if self.review_id is not None and not isinstance(self.review_id, UUID):
            raise SummaryReviewManifestError("review_id must be a UUID or None")

    @property
    def admin_author_id(self) -> UUID:
        """Return the asserted admin author for server-side audit use only."""

        return self.reviewed_by


@dataclass(frozen=True, slots=True)
class SummaryProtectedExcerptRequest:
    """An exact character range in a trusted materialized source.

    Offsets are zero-based Python character offsets (Unicode code points), with
    ``end_offset`` exclusive. The request intentionally contains no claimed
    text: the builder reconstructs only ``source_text[start_offset:end_offset]``.
    """

    fact_ref: str
    source_id: str
    role: SummaryExcerptRole | str
    start_offset: int
    end_offset: int

    def __post_init__(self) -> None:
        _require_identifier(self.fact_ref, "fact_ref")
        _require_identifier(self.source_id, "source_id")
        try:
            role = SummaryExcerptRole(self.role)
        except (TypeError, ValueError):
            raise SummaryReviewManifestError(
                "protected excerpt role must be user or assistant"
            ) from None
        object.__setattr__(self, "role", role)
        _require_non_negative_int(self.start_offset, "start_offset")
        _require_non_negative_int(self.end_offset, "end_offset")
        if self.start_offset >= self.end_offset:
            raise SummaryReviewManifestError("protected excerpt offsets must be non-empty")


@dataclass(frozen=True, slots=True)
class SummaryProtectedSourceAudit:
    """Server-side review metadata for one complete selected source."""

    source_id: str
    source_identity: str
    source_revision_sha256: str
    turn_id: UUID
    run_id: UUID
    sequence: int
    source_watermark: int
    frozen_summary_eligibility: str
    review_id: UUID | None
    reviewed_by: UUID
    review_revision: int
    reviewer_is_workspace_admin: bool
    review_decision: SummaryReviewDecision


@dataclass(frozen=True, slots=True)
class SummaryProtectedExcerptAudit:
    """Server-side provenance for one protected fact."""

    fact_ref: str
    source_id: str
    source_identity: str
    role: SummaryExcerptRole
    start_offset: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class SummaryProtectedReviewAudit:
    """Detached audit metadata that must remain outside model prompt payloads."""

    workspace_id: UUID
    thread_id: UUID
    before_run_id: UUID
    before_sequence: int
    sources: tuple[SummaryProtectedSourceAudit, ...]
    excerpts: tuple[SummaryProtectedExcerptAudit, ...]
    protected_manifest_complete_asserted: bool
    contract_version: str = SUMMARY_REVIEW_MANIFEST_VERSION
    server_revalidation_required: bool = True
    assertions: tuple[str, ...] = SUMMARY_REVIEW_ASSERTIONS

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id, "workspace_id")
        _require_uuid(self.thread_id, "thread_id")
        _require_uuid(self.before_run_id, "before_run_id")
        _require_positive_int(self.before_sequence, "before_sequence")
        sources = _typed_sequence(self.sources, SummaryProtectedSourceAudit, "sources")
        excerpts = _typed_sequence(self.excerpts, SummaryProtectedExcerptAudit, "excerpts")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "excerpts", excerpts)
        if self.protected_manifest_complete_asserted is not True:
            raise SummaryReviewManifestError(
                "protected_manifest_complete must be an explicit true caller assertion"
            )
        if self.contract_version != SUMMARY_REVIEW_MANIFEST_VERSION:
            raise SummaryReviewManifestError("protected review contract version is invalid")
        if self.server_revalidation_required is not True:
            raise SummaryReviewManifestError("server revalidation cannot be disabled")
        if self.assertions != SUMMARY_REVIEW_ASSERTIONS:
            raise SummaryReviewManifestError("protected review assertions are fixed")

    def to_record(self) -> dict[str, object]:
        """Return detached JSON-ready audit data for server-side persistence."""

        return {
            "contract_version": self.contract_version,
            "workspace_id": str(self.workspace_id),
            "thread_id": str(self.thread_id),
            "before_run_id": str(self.before_run_id),
            "before_sequence": self.before_sequence,
            "protected_manifest_complete_asserted": (self.protected_manifest_complete_asserted),
            "server_revalidation_required": self.server_revalidation_required,
            "assertions": list(self.assertions),
            "sources": [
                {
                    "source_id": source.source_id,
                    "source_identity": source.source_identity,
                    "source_revision_sha256": source.source_revision_sha256,
                    "turn_id": str(source.turn_id),
                    "run_id": str(source.run_id),
                    "sequence": source.sequence,
                    "source_watermark": source.source_watermark,
                    "frozen_summary_eligibility": source.frozen_summary_eligibility,
                    "review_id": (str(source.review_id) if source.review_id is not None else None),
                    "reviewed_by": str(source.reviewed_by),
                    "review_revision": source.review_revision,
                    "reviewer_is_workspace_admin": (source.reviewer_is_workspace_admin),
                    "review_decision": source.review_decision.value,
                }
                for source in self.sources
            ],
            "excerpts": [
                {
                    "fact_ref": excerpt.fact_ref,
                    "source_id": excerpt.source_id,
                    "source_identity": excerpt.source_identity,
                    "role": excerpt.role.value,
                    "start_offset": excerpt.start_offset,
                    "end_offset": excerpt.end_offset,
                }
                for excerpt in self.excerpts
            ],
        }


@dataclass(frozen=True, slots=True)
class SummaryProtectedReviewManifest:
    """Reviewed facts plus server-only audit metadata.

    Call ``for_prompt`` at the model boundary. Only the returned
    ``SummaryProtectedFact`` values are compatible with ``SummaryInputContext``.
    Reviewer identity, admin assertions, revisions, offsets, and the audit record
    are deliberately excluded from that prompt-facing tuple.
    """

    audit: SummaryProtectedReviewAudit
    protected_facts: tuple[SummaryProtectedFact, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.audit, SummaryProtectedReviewAudit):
            raise SummaryReviewManifestError("audit must be a protected review audit")
        facts = _typed_sequence(self.protected_facts, SummaryProtectedFact, "protected_facts")
        object.__setattr__(self, "protected_facts", facts)
        fact_refs = tuple(fact.fact_ref for fact in facts)
        if len(fact_refs) != len(set(fact_refs)):
            raise SummaryReviewManifestError("protected fact refs must be unique")

    @property
    def workspace_id(self) -> UUID:
        return self.audit.workspace_id

    @property
    def thread_id(self) -> UUID:
        return self.audit.thread_id

    @property
    def before_run_id(self) -> UUID:
        return self.audit.before_run_id

    @property
    def before_sequence(self) -> int:
        return self.audit.before_sequence

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(source.source_id for source in self.audit.sources)

    def for_prompt(self) -> tuple[SummaryProtectedFact, ...]:
        """Return only the model-compatible protected facts, never audit data."""

        return self.protected_facts


def build_summary_review_manifest(
    *,
    manifest: ThreadHistorySourceManifest,
    materialized_turns: Sequence[ThreadTurnContext],
    source_reviews: Sequence[SummarySourceReviewAssertion],
    excerpts: Sequence[SummaryProtectedExcerptRequest],
    workspace_id: UUID,
    thread_id: UUID,
    before_run_id: UUID,
    before_sequence: int,
    protected_manifest_complete: bool,
) -> SummaryProtectedReviewManifest:
    """Bind independent review assertions to exact protected source excerpts.

    Review eligibility, reviewer admin status, and protected-manifest
    completeness are required caller assertions. They are not permission,
    semantic-completeness, or authorization checks. A real server boundary must
    revalidate all of them before prompt construction or model dispatch.
    """

    if not isinstance(manifest, ThreadHistorySourceManifest):
        raise TypeError("manifest must be a ThreadHistorySourceManifest")
    _validate_scope(
        manifest,
        workspace_id=workspace_id,
        thread_id=thread_id,
        before_run_id=before_run_id,
        before_sequence=before_sequence,
    )
    if manifest.status != HISTORY_SOURCE_COMPLETE:
        raise SummaryReviewManifestError("only a COMPLETE source manifest is supported")
    if (
        manifest.selected_source_count != manifest.available_eligible_source_count
        or manifest.selected_source_count != manifest.available_source_count
    ):
        raise SummaryReviewManifestError("every measured eligible source must be selected")
    if (
        manifest.selected_artifact_count != 0
        or manifest.available_artifact_count != 0
        or not manifest.provenance_complete
    ):
        raise SummaryReviewManifestError("protected review requires complete artifact-free sources")
    if len(manifest.selected_sources) > MAX_SUMMARY_PROTECTED_SOURCES:
        raise SummaryReviewManifestError(
            f"protected review supports at most {MAX_SUMMARY_PROTECTED_SOURCES} sources"
        )
    if protected_manifest_complete is not True:
        raise SummaryReviewManifestError(
            "protected_manifest_complete must be an explicit true caller assertion; "
            "the server must still revalidate semantic completeness"
        )

    turns = _typed_sequence(
        materialized_turns,
        ThreadTurnContext,
        "materialized_turns",
        maximum=MAX_SUMMARY_PROTECTED_SOURCES,
    )
    turn_by_id = _index_turns(turns)
    expected_turn_ids = {source.turn_id for source in manifest.selected_sources}
    if set(turn_by_id) != expected_turn_ids:
        raise SummaryReviewManifestError(
            "materialized Turns must exactly match the complete selected source membership"
        )

    source_bindings: list[tuple[ThreadHistorySourceIdentity, ThreadTurnContext, str]] = []
    source_ids: set[str] = set()
    source_identities: set[str] = set()
    for source in manifest.selected_sources:
        turn = turn_by_id[source.turn_id]
        _validate_supported_source(source, turn)
        try:
            identity = stable_summary_source_identity(source, turn)
        except (TypeError, ValueError) as error:
            raise SummaryReviewManifestError(
                f"source {source.source_id} no longer matches its materialized Turn"
            ) from error
        if source.source_id in source_ids or identity in source_identities:
            raise SummaryReviewManifestError("selected sources must be unique")
        source_ids.add(source.source_id)
        source_identities.add(identity)
        source_bindings.append((source, turn, identity))

    reviews = _typed_sequence(
        source_reviews,
        SummarySourceReviewAssertion,
        "source_reviews",
        maximum=MAX_SUMMARY_PROTECTED_SOURCES,
    )
    reviews_by_identity = _index_reviews(reviews)
    if set(reviews_by_identity) != source_identities:
        missing = source_identities.difference(reviews_by_identity)
        extra = set(reviews_by_identity).difference(source_identities)
        raise SummaryReviewManifestError(
            "source reviews must exactly match selected stable identities "
            f"(missing={len(missing)}, extra={len(extra)})"
        )
    for source, _, identity in source_bindings:
        review = reviews_by_identity[identity]
        if review.decision is not SummaryReviewDecision.ELIGIBLE:
            raise SummaryReviewManifestError(
                f"source {source.source_id} does not have an independent ELIGIBLE review"
            )
        if review.reviewer_is_workspace_admin is not True:
            raise SummaryReviewManifestError(
                f"source {source.source_id} lacks the asserted workspace-admin author"
            )

    binding_by_source_id = {
        source.source_id: (source, turn, identity) for source, turn, identity in source_bindings
    }
    excerpt_requests = _typed_sequence(
        excerpts,
        SummaryProtectedExcerptRequest,
        "excerpts",
        maximum=MAX_SUMMARY_PROTECTED_SOURCES * MAX_SUMMARY_PROTECTED_EXCERPTS_PER_SOURCE,
    )
    facts, excerpt_audits = _build_exact_excerpts(
        excerpt_requests,
        binding_by_source_id=binding_by_source_id,
    )

    source_audits = tuple(
        SummaryProtectedSourceAudit(
            source_id=source.source_id,
            source_identity=identity,
            source_revision_sha256=source.revision_sha256,
            turn_id=source.turn_id,
            run_id=source.run_id,
            sequence=source.sequence,
            source_watermark=source.watermark,
            frozen_summary_eligibility=source.summary_eligibility,
            review_id=reviews_by_identity[identity].review_id,
            reviewed_by=reviews_by_identity[identity].reviewed_by,
            review_revision=reviews_by_identity[identity].revision,
            reviewer_is_workspace_admin=(reviews_by_identity[identity].reviewer_is_workspace_admin),
            review_decision=reviews_by_identity[identity].decision,
        )
        for source, _, identity in source_bindings
    )
    audit = SummaryProtectedReviewAudit(
        workspace_id=workspace_id,
        thread_id=thread_id,
        before_run_id=before_run_id,
        before_sequence=before_sequence,
        sources=source_audits,
        excerpts=excerpt_audits,
        protected_manifest_complete_asserted=True,
    )
    return SummaryProtectedReviewManifest(audit=audit, protected_facts=facts)


def _validate_scope(
    manifest: ThreadHistorySourceManifest,
    *,
    workspace_id: UUID,
    thread_id: UUID,
    before_run_id: UUID,
    before_sequence: int,
) -> None:
    _require_uuid(workspace_id, "workspace_id")
    _require_uuid(thread_id, "thread_id")
    _require_uuid(before_run_id, "before_run_id")
    _require_positive_int(before_sequence, "before_sequence")
    scope = manifest.scope
    actual = (
        scope.workspace_id,
        scope.thread_id,
        scope.before_run_id,
        scope.before_sequence,
    )
    expected = (workspace_id, thread_id, before_run_id, before_sequence)
    if actual != expected:
        raise SummaryReviewManifestError(
            "manifest scope must exactly match workspace_id, thread_id, "
            "before_run_id, and watermark"
        )
    for source in manifest.selected_sources:
        if (
            source.workspace_id != workspace_id
            or source.thread_id != thread_id
            or source.watermark != before_sequence
        ):
            raise SummaryReviewManifestError(
                "selected source escapes the exact protected review scope"
            )


def _validate_supported_source(
    source: ThreadHistorySourceIdentity,
    turn: ThreadTurnContext,
) -> None:
    if source.summary_eligibility == "INELIGIBLE":
        raise SummaryReviewManifestError(
            f"source {source.source_id} is frozen as ineligible and must remain exact"
        )
    if source.artifact_identities:
        raise SummaryReviewManifestError(
            f"source {source.source_id} has artifacts and must remain exact"
        )
    if turn.artifact_ids or turn.artifact_refs:
        raise SummaryReviewManifestError(
            f"source {source.source_id} has artifacts and must remain exact"
        )
    if turn.evidence_registry is not None:
        raise SummaryReviewManifestError(
            f"source {source.source_id} has an evidence registry and must remain exact"
        )
    if turn.source_knowledge_snapshots:
        raise SummaryReviewManifestError(
            f"source {source.source_id} has knowledge snapshots and must remain exact"
        )
    if turn.source_wiki_publications:
        raise SummaryReviewManifestError(
            f"source {source.source_id} has wiki publications and must remain exact"
        )
    if not isinstance(turn.user_input, str) or not turn.user_input.strip():
        raise SummaryReviewManifestError(f"source {source.source_id} has no completed user text")
    if not isinstance(turn.final_output, str) or not turn.final_output.strip():
        raise SummaryReviewManifestError(
            f"source {source.source_id} has no completed assistant text"
        )
    if "\x00" in turn.user_input or "\x00" in turn.final_output:
        raise SummaryReviewManifestError(f"source {source.source_id} contains a NUL character")
    if not isinstance(turn.messages, tuple):
        raise SummaryReviewManifestError(
            f"source {source.source_id} has an invalid transcript container"
        )
    for message in turn.messages:
        if not isinstance(message, ModelMessage):
            raise SummaryReviewManifestError(
                f"source {source.source_id} has an unknown transcript item"
            )
        if message.role not in {"user", "assistant"}:
            raise SummaryReviewManifestError(
                f"source {source.source_id} contains unsupported role {message.role!r}"
            )
        if message.tool_calls or message.tool_call_id is not None:
            raise SummaryReviewManifestError(
                f"source {source.source_id} contains tool activity and must remain exact"
            )
        if not isinstance(message.content, str):
            raise SummaryReviewManifestError(
                f"source {source.source_id} has a transcript item without text"
            )
    if turn.messages and (
        len(turn.messages) != 2
        or turn.messages[0].role != "user"
        or turn.messages[0].content != turn.user_input
        or turn.messages[1].role != "assistant"
        or turn.messages[1].content != turn.final_output
    ):
        raise SummaryReviewManifestError(
            f"source {source.source_id} has an unsupported transcript and must remain exact"
        )


def _build_exact_excerpts(
    requests: tuple[SummaryProtectedExcerptRequest, ...],
    *,
    binding_by_source_id: Mapping[str, tuple[ThreadHistorySourceIdentity, ThreadTurnContext, str]],
) -> tuple[tuple[SummaryProtectedFact, ...], tuple[SummaryProtectedExcerptAudit, ...]]:
    fact_refs: set[str] = set()
    coordinates: set[tuple[str, SummaryExcerptRole, int, int]] = set()
    counts_by_source: dict[str, int] = {}
    facts: list[SummaryProtectedFact] = []
    audits: list[SummaryProtectedExcerptAudit] = []
    for request in requests:
        binding = binding_by_source_id.get(request.source_id)
        if binding is None:
            raise SummaryReviewManifestError(
                f"protected excerpt cites source outside the selected manifest: {request.source_id}"
            )
        count = counts_by_source.get(request.source_id, 0) + 1
        if count > MAX_SUMMARY_PROTECTED_EXCERPTS_PER_SOURCE:
            raise SummaryReviewManifestError(
                f"source {request.source_id} exceeds "
                f"{MAX_SUMMARY_PROTECTED_EXCERPTS_PER_SOURCE} protected excerpts"
            )
        counts_by_source[request.source_id] = count
        if request.fact_ref in fact_refs:
            raise SummaryReviewManifestError(f"duplicate protected fact_ref: {request.fact_ref}")
        coordinate = (
            request.source_id,
            request.role,
            request.start_offset,
            request.end_offset,
        )
        if coordinate in coordinates:
            raise SummaryReviewManifestError(
                f"duplicate protected excerpt coordinate for source {request.source_id}"
            )
        fact_refs.add(request.fact_ref)
        coordinates.add(coordinate)

        source, turn, identity = binding
        source_text = (
            turn.user_input if request.role is SummaryExcerptRole.USER else turn.final_output
        )
        if (
            request.start_offset < 0
            or request.end_offset > len(source_text)
            or request.start_offset >= request.end_offset
        ):
            raise SummaryReviewManifestError(
                f"protected excerpt offsets are outside source {request.source_id}"
            )
        if (
            source_text[request.start_offset].isspace()
            or source_text[request.end_offset - 1].isspace()
        ):
            raise SummaryReviewManifestError("protected excerpt boundaries must not be whitespace")
        text = source_text[request.start_offset : request.end_offset]
        if not text or text != text.strip():
            raise SummaryReviewManifestError(
                "protected excerpt must be non-empty and have trimmed boundaries"
            )
        try:
            fact = SummaryProtectedFact(
                fact_ref=request.fact_ref,
                text=text,
                source_ids=(source.source_id,),
            )
        except SummaryInputContextError as error:
            raise SummaryReviewManifestError(
                f"protected fact {request.fact_ref} is invalid: {error}"
            ) from error
        facts.append(fact)
        audits.append(
            SummaryProtectedExcerptAudit(
                fact_ref=request.fact_ref,
                source_id=source.source_id,
                source_identity=identity,
                role=request.role,
                start_offset=request.start_offset,
                end_offset=request.end_offset,
            )
        )
    return tuple(facts), tuple(audits)


def _index_turns(
    turns: tuple[ThreadTurnContext, ...],
) -> dict[UUID, ThreadTurnContext]:
    result: dict[UUID, ThreadTurnContext] = {}
    for turn in turns:
        if not isinstance(turn.turn_id, UUID) or not isinstance(turn.run_id, UUID):
            raise SummaryReviewManifestError(
                "materialized Turns require UUID turn_id and run_id values"
            )
        if turn.turn_id in result:
            raise SummaryReviewManifestError("materialized Turns contain a duplicate turn_id")
        result[turn.turn_id] = turn
    return result


def _index_reviews(
    reviews: tuple[SummarySourceReviewAssertion, ...],
) -> dict[str, SummarySourceReviewAssertion]:
    result: dict[str, SummarySourceReviewAssertion] = {}
    for review in reviews:
        if review.source_identity in result:
            raise SummaryReviewManifestError("source reviews contain a duplicate stable identity")
        result[review.source_identity] = review
    return result


def _typed_sequence(
    values: Sequence[object],
    expected: type[object],
    name: str,
    *,
    maximum: int | None = None,
) -> tuple[object, ...]:
    if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Sequence):
        raise SummaryReviewManifestError(f"{name} must be a sequence")
    if maximum is not None and len(values) > maximum:
        raise SummaryReviewManifestError(f"{name} exceeds the bound of {maximum}")
    result = tuple(values)
    if any(not isinstance(value, expected) for value in result):
        raise SummaryReviewManifestError(f"{name} contains an invalid item")
    return result


def _require_identifier(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_SUMMARY_REVIEW_IDENTIFIER_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SummaryReviewManifestError(
            f"{name} must be a trimmed, control-free identifier of at most "
            f"{MAX_SUMMARY_REVIEW_IDENTIFIER_LENGTH} characters"
        )
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_identifier(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SummaryReviewManifestError(f"{name} must be a lowercase SHA-256 hex digest")
    return text


def _require_uuid(value: object, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise SummaryReviewManifestError(f"{name} must be a UUID")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SummaryReviewManifestError(f"{name} must be a positive integer")
    return value


def _require_positive_bounded_int(
    value: object,
    name: str,
    *,
    maximum: int,
) -> int:
    integer = _require_positive_int(value, name)
    if integer > maximum:
        raise SummaryReviewManifestError(f"{name} must be at most {maximum}")
    return integer


def _require_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SummaryReviewManifestError(f"{name} must be a non-negative integer")
    return value


__all__ = [
    "MAX_SUMMARY_PROTECTED_EXCERPTS_PER_SOURCE",
    "MAX_SUMMARY_PROTECTED_SOURCES",
    "MAX_SUMMARY_REVIEW_IDENTIFIER_LENGTH",
    "MAX_SUMMARY_REVIEW_REVISION",
    "SUMMARY_REVIEW_ASSERTIONS",
    "SUMMARY_REVIEW_MANIFEST_VERSION",
    "SummaryExcerptRole",
    "SummaryProtectedExcerptAudit",
    "SummaryProtectedExcerptRequest",
    "SummaryProtectedReviewAudit",
    "SummaryProtectedReviewManifest",
    "SummaryProtectedSourceAudit",
    "SummaryReviewDecision",
    "SummaryReviewManifestError",
    "SummarySourceReviewAssertion",
    "build_summary_review_manifest",
]
