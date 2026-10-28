"""The two seams the work layer plugs into the runtime through.

Threads and artifacts sit *above* the runtime: they read agent runs, so they
depend on this package and this package must not depend on them. The runtime
therefore declares what it needs as protocols here, and composition wires the
real implementations in. Without a provider the runtime behaves exactly as it
did before threads existed, which is also what keeps Playground runs unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from packages.core.canonical.json_hash import canonical_json_hash
from packages.model_gateway.contracts import ModelMessage

HISTORY_SOURCE_CONTRACT_VERSION = 1
HISTORY_SOURCE_POLICY = "deterministic_history_v1"
HISTORY_SOURCE_COUNT_VERSION = 1
HISTORY_SOURCE_COMPLETE = "COMPLETE"
HISTORY_SOURCE_LIMIT_REACHED = "LIMIT_REACHED"
HISTORY_SOURCE_TURN_SCAN_LIMIT = "TURN_SCAN_LIMIT_REACHED"
HISTORY_SOURCE_TURN_SELECTION_LIMIT = "TURN_SELECTION_LIMIT_REACHED"
HISTORY_SOURCE_ARTIFACT_LIMIT = "ARTIFACT_LIMIT_REACHED"
HISTORY_SOURCE_SUMMARY_ELIGIBILITY_UNKNOWN = "UNKNOWN"
HISTORY_SOURCE_SUMMARY_REASON_NOT_CLASSIFIED = "PLANNER_SEMANTIC_ELIGIBILITY_NOT_CLASSIFIED"
PLAIN_TEXT_PREFIX_V1 = "plain_text_prefix_v1"
PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON = "PLAIN_TEXT_PREFIX_V1_SIMPLE_COMPLETED_EXCHANGE"
PLAIN_TEXT_PREFIX_V1_BLOCKER_EVIDENCE_UNAVAILABLE = (
    "PLAIN_TEXT_PREFIX_V1_BLOCKER_EVIDENCE_UNAVAILABLE"
)
PLAIN_TEXT_PREFIX_V1_TRANSCRIPT_UNAVAILABLE = "PLAIN_TEXT_PREFIX_V1_TRANSCRIPT_UNAVAILABLE"


class PlainTextPrefixProofStatus(StrEnum):
    """A persisted source fact visible to the opt-in M4 source policy."""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNPROVEN = "UNPROVEN"


@dataclass(frozen=True, slots=True)
class PlainTextPrefixProof:
    """Server-owned proof for the bounded ``plain_text_prefix_v1`` policy.

    Only the thread-context adapter populates this value after reading the
    persisted Run and its approval/action evidence. ``UNPROVEN`` is distinct
    from an empty transcript: an absent or empty ``messages`` tuple alone is
    not evidence that no external action occurred.
    """

    status: PlainTextPrefixProofStatus | str
    reason: str
    policy: str = PLAIN_TEXT_PREFIX_V1

    def __post_init__(self) -> None:
        try:
            status = PlainTextPrefixProofStatus(self.status)
        except (TypeError, ValueError):
            raise ValueError("plain-text proof status is invalid") from None
        object.__setattr__(self, "status", status)
        if self.policy != PLAIN_TEXT_PREFIX_V1:
            raise ValueError(f"plain-text proof policy must be {PLAIN_TEXT_PREFIX_V1}")
        _trimmed_text(self.reason, "plain-text proof reason")


# These are contract ceilings, not tuning knobs. Callers may lower them for a
# run. The extra row is a sentinel used to prove that a scan ending exactly at
# ``max_turns_scanned`` was exhaustive; it is never selected or retained.
HISTORY_SOURCE_MAX_TURNS = 200
HISTORY_SOURCE_MAX_TURNS_SCANNED = 2_000
HISTORY_SOURCE_MAX_ARTIFACTS = 5_000
HISTORY_SOURCE_MAX_PAGE_SIZE = 500


class ThreadHistorySourceIntegrityError(ValueError):
    """A frozen source identity can no longer be reconstructed exactly."""

    code = "THREAD_HISTORY_SOURCE_INTEGRITY_ERROR"


class ThreadHistorySourceIncompleteError(ThreadHistorySourceIntegrityError):
    """A source exists but its complete conversation cannot be reconstructed."""


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _trimmed_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _sha256_text(value: Any, name: str) -> str:
    text = _trimmed_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return text


def _uuid_value(value: Any, name: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{name} must be a UUID") from None


@dataclass(frozen=True, slots=True)
class ThreadTurnContext:
    """One completed earlier turn, as the next turn should see it."""

    user_input: str
    final_output: str
    turn_id: UUID | None = None
    run_id: UUID | None = None
    sequence: int | None = None
    artifact_ids: tuple[UUID, ...] = ()
    # One line per artifact, never the artifact itself: twenty abstracts would
    # eat the budget to tell the model something one line already tells it.
    artifact_refs: tuple[str, ...] = ()
    # Present only when the complete model-visible exchange was persisted.
    messages: tuple[ModelMessage, ...] = ()
    evidence_registry: dict[str, Any] | None = None
    source_knowledge_snapshots: tuple[dict[str, Any], ...] = ()
    source_wiki_publications: tuple[dict[str, Any], ...] = ()
    # Populated only by the persisted thread-context adapter. Callers must not
    # infer this from an empty transcript or from user-controlled input.
    plain_text_prefix_v1_proof: PlainTextPrefixProof | None = None


@dataclass(frozen=True, slots=True)
class ThreadConversation:
    turns: tuple[ThreadTurnContext, ...] = ()
    # What existed before the "most recent N" cut, so the drop is reportable
    # rather than invisible. ``turns_available_is_exact`` is false when a work
    # cap stopped measurement before the frozen watermark was exhausted.
    turns_available: int = 0
    turns_available_is_exact: bool = True
    source_manifest: ThreadHistorySourceManifest | None = None


@dataclass(frozen=True, slots=True)
class ThreadHistorySourceScope:
    """The immutable coordinate in which every source must be interpreted."""

    workspace_id: UUID
    thread_id: UUID
    before_run_id: UUID
    before_sequence: int
    max_turns: int
    max_turns_scanned: int
    max_artifacts: int
    artifact_page_size: int
    count_version: int = HISTORY_SOURCE_COUNT_VERSION
    policy: str = HISTORY_SOURCE_POLICY
    contract_version: int = HISTORY_SOURCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for field_name in ("workspace_id", "thread_id", "before_run_id"):
            if not isinstance(getattr(self, field_name), UUID):
                raise TypeError(f"{field_name} must be a UUID")
        for field_name in (
            "before_sequence",
            "max_turns",
            "max_turns_scanned",
            "max_artifacts",
            "artifact_page_size",
            "count_version",
        ):
            _positive_int(getattr(self, field_name), field_name)
        if self.max_turns > HISTORY_SOURCE_MAX_TURNS:
            raise ValueError(f"max_turns must be at most {HISTORY_SOURCE_MAX_TURNS}")
        if self.max_turns_scanned > HISTORY_SOURCE_MAX_TURNS_SCANNED:
            raise ValueError(
                f"max_turns_scanned must be at most {HISTORY_SOURCE_MAX_TURNS_SCANNED}"
            )
        if self.max_turns_scanned < self.max_turns:
            raise ValueError("max_turns_scanned cannot be below max_turns")
        if self.max_artifacts > HISTORY_SOURCE_MAX_ARTIFACTS:
            raise ValueError(f"max_artifacts must be at most {HISTORY_SOURCE_MAX_ARTIFACTS}")
        if self.artifact_page_size > HISTORY_SOURCE_MAX_PAGE_SIZE:
            raise ValueError(f"artifact_page_size must be at most {HISTORY_SOURCE_MAX_PAGE_SIZE}")
        if self.count_version != HISTORY_SOURCE_COUNT_VERSION:
            raise ValueError(f"count_version must be {HISTORY_SOURCE_COUNT_VERSION}")
        if self.policy != HISTORY_SOURCE_POLICY:
            raise ValueError(f"policy must be {HISTORY_SOURCE_POLICY}")
        if self.contract_version != HISTORY_SOURCE_CONTRACT_VERSION:
            raise ValueError(f"contract_version must be {HISTORY_SOURCE_CONTRACT_VERSION}")

    def to_manifest(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "policy": self.policy,
            "workspace_id": str(self.workspace_id),
            "thread_id": str(self.thread_id),
            "before_run_id": str(self.before_run_id),
            "before_sequence": self.before_sequence,
            "max_turns": self.max_turns,
            "max_turns_scanned": self.max_turns_scanned,
            "max_artifacts": self.max_artifacts,
            "artifact_page_size": self.artifact_page_size,
            "count_version": self.count_version,
        }


@dataclass(frozen=True, slots=True)
class ThreadHistoryArtifactIdentity:
    """The stable ID and canonical content identity of one persisted artifact."""

    artifact_id: UUID
    content_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, UUID):
            raise TypeError("artifact_id must be a UUID")
        _sha256_text(self.content_sha256, "artifact.content_sha256")

    def to_manifest(self) -> dict[str, Any]:
        return {
            "artifact_id": str(self.artifact_id),
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_manifest(cls, value: Any) -> ThreadHistoryArtifactIdentity:
        required = {"artifact_id", "content_sha256"}
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("artifact identity has an invalid shape")
        return cls(
            artifact_id=_uuid_value(value["artifact_id"], "artifact_id"),
            content_sha256=_sha256_text(value["content_sha256"], "artifact.content_sha256"),
        )


@dataclass(frozen=True, slots=True)
class ThreadHistorySourceIdentity:
    """One complete user/assistant exchange plus its persisted provenance."""

    source_id: str
    workspace_id: UUID
    thread_id: UUID
    turn_id: UUID
    run_id: UUID
    sequence: int
    watermark: int
    exchange_id: str
    original_ref: str
    count_version: int
    user_input_sha256: str
    final_output_sha256: str
    revision_sha256: str
    artifact_identities: tuple[ThreadHistoryArtifactIdentity, ...] = ()
    summary_eligibility: str = HISTORY_SOURCE_SUMMARY_ELIGIBILITY_UNKNOWN
    summary_eligibility_reason: str = HISTORY_SOURCE_SUMMARY_REASON_NOT_CLASSIFIED

    def __post_init__(self) -> None:
        if self.source_id != f"thread-turn:{self.turn_id}":
            raise ValueError("source_id does not match the persisted turn identity")
        if self.exchange_id != self.source_id:
            raise ValueError("exchange_id must bind the complete turn exchange")
        if self.original_ref != self.source_id:
            raise ValueError("original_ref must resolve the persisted turn")
        for field_name in ("workspace_id", "thread_id", "turn_id", "run_id"):
            if not isinstance(getattr(self, field_name), UUID):
                raise TypeError(f"{field_name} must be a UUID")
        _positive_int(self.sequence, "sequence")
        _positive_int(self.watermark, "watermark")
        if self.watermark <= self.sequence:
            raise ValueError("source must be strictly below its watermark")
        if self.count_version != HISTORY_SOURCE_COUNT_VERSION:
            raise ValueError(f"count_version must be {HISTORY_SOURCE_COUNT_VERSION}")
        _sha256_text(self.user_input_sha256, "user_input_sha256")
        _sha256_text(self.final_output_sha256, "final_output_sha256")
        _sha256_text(self.revision_sha256, "revision_sha256")
        if not isinstance(self.artifact_identities, tuple) or any(
            not isinstance(item, ThreadHistoryArtifactIdentity) for item in self.artifact_identities
        ):
            raise TypeError("artifact_identities must be a tuple of artifact identities")
        artifact_ids = tuple(item.artifact_id for item in self.artifact_identities)
        if artifact_ids != tuple(sorted(set(artifact_ids))):
            raise ValueError("artifact identities must be unique and ID-sorted")
        if self.summary_eligibility not in {"ELIGIBLE", "INELIGIBLE", "UNKNOWN"}:
            raise ValueError("summary_eligibility is invalid")
        _trimmed_text(
            self.summary_eligibility_reason,
            "summary_eligibility_reason",
        )

    @property
    def artifact_ids(self) -> tuple[UUID, ...]:
        return tuple(item.artifact_id for item in self.artifact_identities)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "workspace_id": str(self.workspace_id),
            "thread_id": str(self.thread_id),
            "turn_id": str(self.turn_id),
            "run_id": str(self.run_id),
            "sequence": self.sequence,
            "watermark": self.watermark,
            "exchange_id": self.exchange_id,
            "original_ref": self.original_ref,
            "count_version": self.count_version,
            "user_input_sha256": self.user_input_sha256,
            "final_output_sha256": self.final_output_sha256,
            "revision_sha256": self.revision_sha256,
            "artifact_identities": [item.to_manifest() for item in self.artifact_identities],
            "summary_eligibility": self.summary_eligibility,
            "summary_eligibility_reason": self.summary_eligibility_reason,
        }

    @classmethod
    def from_manifest(cls, value: Any) -> ThreadHistorySourceIdentity:
        required = {
            "source_id",
            "workspace_id",
            "thread_id",
            "turn_id",
            "run_id",
            "sequence",
            "watermark",
            "exchange_id",
            "original_ref",
            "count_version",
            "user_input_sha256",
            "final_output_sha256",
            "revision_sha256",
            "artifact_identities",
            "summary_eligibility",
            "summary_eligibility_reason",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("source identity has an invalid shape")
        artifact_values = value["artifact_identities"]
        if not isinstance(artifact_values, list):
            raise ValueError("artifact_identities must be a list")
        return cls(
            source_id=_trimmed_text(value["source_id"], "source_id"),
            workspace_id=_uuid_value(value["workspace_id"], "workspace_id"),
            thread_id=_uuid_value(value["thread_id"], "thread_id"),
            turn_id=_uuid_value(value["turn_id"], "turn_id"),
            run_id=_uuid_value(value["run_id"], "run_id"),
            sequence=_positive_int(value["sequence"], "sequence"),
            watermark=_positive_int(value["watermark"], "watermark"),
            exchange_id=_trimmed_text(value["exchange_id"], "exchange_id"),
            original_ref=_trimmed_text(value["original_ref"], "original_ref"),
            count_version=_positive_int(value["count_version"], "count_version"),
            user_input_sha256=_sha256_text(value["user_input_sha256"], "user_input_sha256"),
            final_output_sha256=_sha256_text(value["final_output_sha256"], "final_output_sha256"),
            revision_sha256=_sha256_text(value["revision_sha256"], "revision_sha256"),
            artifact_identities=tuple(
                ThreadHistoryArtifactIdentity.from_manifest(item) for item in artifact_values
            ),
            summary_eligibility=_trimmed_text(value["summary_eligibility"], "summary_eligibility"),
            summary_eligibility_reason=_trimmed_text(
                value["summary_eligibility_reason"],
                "summary_eligibility_reason",
            ),
        )


@dataclass(frozen=True, slots=True)
class ThreadHistorySourceManifest:
    """Versioned, persistable manifest for one deterministic history snapshot.

    Source counts describe the eligible Turn/Run exchanges below the frozen
    watermark. Artifact counts describe the selected source window only: reading
    artifacts for a Turn prefix that ``max_turns`` deliberately omits would make
    the source contract's work bound meaningless. ``LOWER_BOUND`` means the
    corresponding work cap stopped measurement, so the integer is a proven
    minimum rather than a claim that the whole watermark was measured.
    """

    scope: ThreadHistorySourceScope
    status: Literal["COMPLETE", "LIMIT_REACHED"]
    selected_sources: tuple[ThreadHistorySourceIdentity, ...]
    selected_source_count: int
    available_eligible_source_count: int
    selected_artifact_count: int
    available_artifact_count: int | None
    available_artifact_count_scope: str
    available_source_count: int | None
    available_source_count_scope: str
    provenance_complete: bool
    below_watermark_has_eligible_source: bool
    work_limit_reached: bool
    limit_reason: str | None = None
    detail: str | None = None
    manifest_sha256: str = ""
    contract_version: int = HISTORY_SOURCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != HISTORY_SOURCE_CONTRACT_VERSION:
            raise ValueError(f"contract_version must be {HISTORY_SOURCE_CONTRACT_VERSION}")
        if self.status not in {HISTORY_SOURCE_COMPLETE, HISTORY_SOURCE_LIMIT_REACHED}:
            raise ValueError("status is invalid")
        if not isinstance(self.selected_sources, tuple) or any(
            not isinstance(item, ThreadHistorySourceIdentity) for item in self.selected_sources
        ):
            raise TypeError("selected_sources must be a tuple of source identities")
        _nonnegative_int(self.selected_source_count, "selected_source_count")
        _nonnegative_int(
            self.available_eligible_source_count,
            "available_eligible_source_count",
        )
        if self.selected_source_count > self.available_eligible_source_count:
            raise ValueError("available_eligible_source_count cannot be below selected sources")
        if self.selected_source_count > self.scope.max_turns:
            raise ValueError("selected sources exceed the scope turn bound")
        _nonnegative_int(self.selected_artifact_count, "selected_artifact_count")
        if self.selected_source_count != len(self.selected_sources):
            raise ValueError("selected_source_count does not match selected sources")
        if self.selected_artifact_count != sum(
            len(source.artifact_identities) for source in self.selected_sources
        ):
            raise ValueError("selected_artifact_count does not match selected sources")
        if self.selected_artifact_count > self.scope.max_artifacts:
            raise ValueError("selected artifacts exceed the scope artifact bound")
        if self.available_artifact_count is not None:
            _nonnegative_int(
                self.available_artifact_count,
                "available_artifact_count",
            )
            if self.available_artifact_count < self.selected_artifact_count:
                raise ValueError("available artifact count cannot be below selected artifacts")
        if self.available_artifact_count_scope not in {
            "FROZEN_WATERMARK",
            "LOWER_BOUND",
        }:
            raise ValueError("available_artifact_count_scope is invalid")
        sequences = [source.sequence for source in self.selected_sources]
        source_ids = [source.source_id for source in self.selected_sources]
        source_keys = [(source.sequence, source.source_id) for source in self.selected_sources]
        if source_keys != sorted(set(source_keys)):
            raise ValueError("selected sources must be in stable, unique sequence order")
        if len(sequences) != len(set(sequences)):
            raise ValueError("selected source sequences must be unique")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("selected source IDs must be unique")
        if len({source.run_id for source in self.selected_sources}) != len(self.selected_sources):
            raise ValueError("selected source run IDs must be unique")
        for source in self.selected_sources:
            if (
                source.workspace_id != self.scope.workspace_id
                or source.thread_id != self.scope.thread_id
                or source.sequence >= self.scope.before_sequence
                or source.watermark != self.scope.before_sequence
            ):
                raise ValueError("selected source escapes the frozen scope")
        if self.available_source_count is not None:
            _nonnegative_int(self.available_source_count, "available_source_count")
            if self.available_source_count < self.available_eligible_source_count:
                raise ValueError("available source count cannot be below the observed count")
        if self.available_source_count_scope not in {
            "FROZEN_WATERMARK",
            "LOWER_BOUND",
        }:
            raise ValueError("available_source_count_scope is invalid")
        if self.status == HISTORY_SOURCE_COMPLETE:
            if self.available_source_count is None:
                raise ValueError("COMPLETE requires a measured source count")
            if self.available_source_count != self.available_eligible_source_count:
                raise ValueError("COMPLETE count must equal the observed candidate count")
            if self.selected_source_count != self.available_source_count:
                raise ValueError("COMPLETE requires all measured sources")
            if self.below_watermark_has_eligible_source:
                raise ValueError("COMPLETE cannot omit eligible sources")
            if self.available_source_count_scope != "FROZEN_WATERMARK":
                raise ValueError("COMPLETE count must cover the frozen watermark")
            if not self.provenance_complete:
                raise ValueError("COMPLETE requires complete artifact provenance")
            if self.work_limit_reached or self.limit_reason is not None:
                raise ValueError("COMPLETE cannot carry a work-limit marker")
            if self.detail is not None:
                raise ValueError("COMPLETE cannot carry incomplete detail")
            if self.available_artifact_count != self.selected_artifact_count:
                raise ValueError("COMPLETE requires an exact, fully selected artifact count")
            if self.available_artifact_count_scope != "FROZEN_WATERMARK":
                raise ValueError("COMPLETE artifact count must be exact")
        else:
            if self.limit_reason not in {
                HISTORY_SOURCE_TURN_SCAN_LIMIT,
                HISTORY_SOURCE_TURN_SELECTION_LIMIT,
                HISTORY_SOURCE_ARTIFACT_LIMIT,
            }:
                raise ValueError("LIMIT_REACHED requires a stable limit reason")
            if not self.detail:
                raise ValueError("LIMIT_REACHED requires a detail")
            scan_limit = self.limit_reason == HISTORY_SOURCE_TURN_SCAN_LIMIT
            selection_limit = self.limit_reason == HISTORY_SOURCE_TURN_SELECTION_LIMIT
            artifact_limit = self.limit_reason == HISTORY_SOURCE_ARTIFACT_LIMIT
            if scan_limit:
                if (
                    self.available_source_count is not None
                    or self.available_source_count_scope != "LOWER_BOUND"
                    or not self.work_limit_reached
                ):
                    raise ValueError("turn scan limits must expose only a lower-bound source count")
            elif (
                self.available_source_count is None
                or self.available_source_count_scope != "FROZEN_WATERMARK"
            ):
                raise ValueError("artifact-limited snapshots must retain the measured source count")
            if selection_limit:
                if (
                    self.available_source_count is None
                    or self.available_source_count <= self.scope.max_turns
                    or self.work_limit_reached
                ):
                    raise ValueError("turn selection limits require an exact overflow count")
            if artifact_limit == self.provenance_complete:
                raise ValueError("artifact limit state does not match provenance completeness")
            if artifact_limit:
                if (
                    self.available_artifact_count is None
                    or self.available_artifact_count <= self.selected_artifact_count
                    or self.available_artifact_count_scope != "LOWER_BOUND"
                ):
                    raise ValueError("artifact limits require an explicit overflow lower bound")
            elif (
                self.available_artifact_count != self.selected_artifact_count
                or self.available_artifact_count_scope != "FROZEN_WATERMARK"
            ):
                raise ValueError("a turn-only limit requires complete artifact provenance")
            if not scan_limit and self.work_limit_reached:
                raise ValueError("work_limit_reached requires a turn scan limit")
        computed = self._canonical_sha256()
        if self.manifest_sha256 and self.manifest_sha256 != computed:
            raise ValueError("manifest_sha256 does not match the manifest")
        object.__setattr__(self, "manifest_sha256", computed)

    def _canonical_sha256(self) -> str:
        return canonical_json_hash(self.payload(include_manifest_hash=False))

    def payload(
        self,
        *,
        include_manifest_hash: bool = True,
        include_contract_version: bool = True,
    ) -> dict[str, Any]:
        scope = self.scope.to_manifest()
        if not include_contract_version:
            scope.pop("contract_version")
        result: dict[str, Any] = {
            "scope": scope,
            "status": self.status,
            "selected_sources": [source.to_manifest() for source in self.selected_sources],
            "selected_source_count": self.selected_source_count,
            "available_eligible_source_count": self.available_eligible_source_count,
            "selected_artifact_count": self.selected_artifact_count,
            "available_artifact_count": self.available_artifact_count,
            "available_artifact_count_scope": self.available_artifact_count_scope,
            "available_source_count": self.available_source_count,
            "available_source_count_scope": self.available_source_count_scope,
            "provenance_complete": self.provenance_complete,
            "below_watermark_has_eligible_source": (self.below_watermark_has_eligible_source),
            "work_limit_reached": self.work_limit_reached,
            "limit_reason": self.limit_reason,
            "detail": self.detail,
        }
        if include_contract_version:
            result["contract_version"] = self.contract_version
        if include_manifest_hash:
            result["manifest_sha256"] = self.manifest_sha256
        return result

    def to_manifest(self) -> dict[str, Any]:
        """Return detached JSON data suitable for AgentRun persistence."""

        return self.payload()

    @classmethod
    def empty(
        cls,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        before_run_id: UUID,
        before_sequence: int | None = None,
        max_turns: int,
        max_turns_scanned: int,
        max_artifacts: int,
        artifact_page_size: int,
        count_version: int = HISTORY_SOURCE_COUNT_VERSION,
    ) -> ThreadHistorySourceManifest:
        """A measured empty view, distinct from "not yet frozen" (``NULL``)."""

        return cls(
            scope=ThreadHistorySourceScope(
                workspace_id=workspace_id,
                thread_id=thread_id,
                before_run_id=before_run_id,
                before_sequence=before_sequence or 1,
                max_turns=max_turns,
                max_turns_scanned=max_turns_scanned,
                max_artifacts=max_artifacts,
                artifact_page_size=artifact_page_size,
                count_version=count_version,
            ),
            status=HISTORY_SOURCE_COMPLETE,
            selected_sources=(),
            selected_source_count=0,
            available_eligible_source_count=0,
            selected_artifact_count=0,
            available_artifact_count=0,
            available_artifact_count_scope="FROZEN_WATERMARK",
            available_source_count=0,
            available_source_count_scope="FROZEN_WATERMARK",
            provenance_complete=True,
            below_watermark_has_eligible_source=False,
            work_limit_reached=False,
        )

    @classmethod
    def from_manifest(cls, value: Any) -> ThreadHistorySourceManifest:
        required = {
            "contract_version",
            "scope",
            "status",
            "selected_sources",
            "selected_source_count",
            "available_eligible_source_count",
            "selected_artifact_count",
            "available_artifact_count",
            "available_artifact_count_scope",
            "available_source_count",
            "available_source_count_scope",
            "provenance_complete",
            "below_watermark_has_eligible_source",
            "work_limit_reached",
            "limit_reason",
            "detail",
            "manifest_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("history source manifest has an invalid shape")
        scope_value = value["scope"]
        scope_required = {
            "contract_version",
            "policy",
            "workspace_id",
            "thread_id",
            "before_run_id",
            "before_sequence",
            "max_turns",
            "max_turns_scanned",
            "max_artifacts",
            "artifact_page_size",
            "count_version",
        }
        if not isinstance(scope_value, Mapping) or set(scope_value) != scope_required:
            raise ValueError("history source scope has an invalid shape")
        scope = ThreadHistorySourceScope(
            contract_version=_positive_int(
                scope_value["contract_version"], "scope.contract_version"
            ),
            policy=_trimmed_text(scope_value["policy"], "scope.policy"),
            workspace_id=_uuid_value(scope_value["workspace_id"], "scope.workspace_id"),
            thread_id=_uuid_value(scope_value["thread_id"], "scope.thread_id"),
            before_run_id=_uuid_value(scope_value["before_run_id"], "scope.before_run_id"),
            before_sequence=_positive_int(scope_value["before_sequence"], "scope.before_sequence"),
            max_turns=_positive_int(scope_value["max_turns"], "scope.max_turns"),
            max_turns_scanned=_positive_int(
                scope_value["max_turns_scanned"], "scope.max_turns_scanned"
            ),
            max_artifacts=_positive_int(scope_value["max_artifacts"], "scope.max_artifacts"),
            artifact_page_size=_positive_int(
                scope_value["artifact_page_size"], "scope.artifact_page_size"
            ),
            count_version=_positive_int(scope_value["count_version"], "scope.count_version"),
        )
        selected_sources = value["selected_sources"]
        if not isinstance(selected_sources, list):
            raise ValueError("selected_sources must be a list")
        provenance_complete = value["provenance_complete"]
        if not isinstance(provenance_complete, bool):
            raise ValueError("provenance_complete must be a boolean")
        below_watermark_has_eligible_source = value["below_watermark_has_eligible_source"]
        if not isinstance(below_watermark_has_eligible_source, bool):
            raise ValueError("below_watermark_has_eligible_source must be a boolean")
        work_limit_reached = value["work_limit_reached"]
        if not isinstance(work_limit_reached, bool):
            raise ValueError("work_limit_reached must be a boolean")
        detail = value["detail"]
        if detail is not None:
            detail = _trimmed_text(detail, "detail")
        limit_reason = value["limit_reason"]
        if limit_reason is not None:
            limit_reason = _trimmed_text(limit_reason, "limit_reason")
        return cls(
            contract_version=_positive_int(value["contract_version"], "contract_version"),
            scope=scope,
            status=_trimmed_text(value["status"], "status"),
            selected_sources=tuple(
                ThreadHistorySourceIdentity.from_manifest(item) for item in selected_sources
            ),
            selected_source_count=_nonnegative_int(
                value["selected_source_count"], "selected_source_count"
            ),
            available_eligible_source_count=_nonnegative_int(
                value["available_eligible_source_count"],
                "available_eligible_source_count",
            ),
            selected_artifact_count=_nonnegative_int(
                value["selected_artifact_count"], "selected_artifact_count"
            ),
            available_artifact_count=(
                None
                if value["available_artifact_count"] is None
                else _nonnegative_int(
                    value["available_artifact_count"],
                    "available_artifact_count",
                )
            ),
            available_artifact_count_scope=_trimmed_text(
                value["available_artifact_count_scope"],
                "available_artifact_count_scope",
            ),
            available_source_count=(
                None
                if value["available_source_count"] is None
                else _nonnegative_int(value["available_source_count"], "available_source_count")
            ),
            available_source_count_scope=_trimmed_text(
                value["available_source_count_scope"],
                "available_source_count_scope",
            ),
            provenance_complete=provenance_complete,
            below_watermark_has_eligible_source=below_watermark_has_eligible_source,
            work_limit_reached=work_limit_reached,
            limit_reason=limit_reason,
            detail=detail,
            manifest_sha256=_sha256_text(value["manifest_sha256"], "manifest_sha256"),
        )


class ThreadContextProvider(Protocol):
    """Supplies the earlier conversation a threaded run should start from."""

    async def conversation(
        self, *, workspace_id: UUID, thread_id: UUID, before_run_id: UUID, max_turns: int
    ) -> ThreadConversation: ...

    async def history_sources(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        before_run_id: UUID,
        max_turns: int,
        max_turns_scanned: int,
        max_artifacts: int,
        artifact_page_size: int,
        summary_source_policy: str | None = None,
    ) -> ThreadConversation: ...

    async def replay_history_sources(
        self, *, manifest: Mapping[str, Any], summary_source_policy: str | None = None
    ) -> ThreadConversation: ...


@dataclass(frozen=True, slots=True)
class ThreadHistoryHit:
    """One earlier turn that matched a search, as the model should see it."""

    sequence: int
    user_input: str
    final_output: str
    matched_terms: tuple[str, ...] = ()


class ThreadHistorySearcher(Protocol):
    """Looks further back in the thread than the replay window reaches.

    The window is a budget decision, not a statement about what matters. When
    the model needs turn three of a forty-turn thread it has exactly two
    options: make something up, or ask. This seam is the asking.
    """

    async def search(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        before_run_id: UUID,
        query: str,
        limit: int,
    ) -> tuple[ThreadHistoryHit, ...]: ...


@dataclass(frozen=True, slots=True)
class RecordedToolCall:
    """One completed tool call, offered to the work layer to keep or ignore."""

    tool_identity: str
    tool_call_id: str
    step_sequence: int
    arguments: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    # Checkpoint coordinates supplied by the runtime, independent of audit
    # sequence increments and provider-generated tool call identifiers.
    logical_call_id: str | None = None


class RunArtifactRecorder(Protocol):
    """Turns tool results into artifacts, if it recognizes any of them.

    The model never writes an artifact. It decides what to search for; what
    came back is copied from the tool result by this recorder, so a fabricated
    paper has no path into the record.
    """

    async def record(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        run_id: UUID,
        created_by: UUID,
        calls: tuple[RecordedToolCall, ...],
    ) -> None: ...


__all__ = [
    "HISTORY_SOURCE_COMPLETE",
    "HISTORY_SOURCE_CONTRACT_VERSION",
    "HISTORY_SOURCE_LIMIT_REACHED",
    "HISTORY_SOURCE_ARTIFACT_LIMIT",
    "HISTORY_SOURCE_COUNT_VERSION",
    "HISTORY_SOURCE_MAX_ARTIFACTS",
    "HISTORY_SOURCE_MAX_PAGE_SIZE",
    "HISTORY_SOURCE_MAX_TURNS",
    "HISTORY_SOURCE_MAX_TURNS_SCANNED",
    "HISTORY_SOURCE_POLICY",
    "HISTORY_SOURCE_SUMMARY_ELIGIBILITY_UNKNOWN",
    "HISTORY_SOURCE_SUMMARY_REASON_NOT_CLASSIFIED",
    "HISTORY_SOURCE_TURN_SCAN_LIMIT",
    "HISTORY_SOURCE_TURN_SELECTION_LIMIT",
    "PLAIN_TEXT_PREFIX_V1",
    "PLAIN_TEXT_PREFIX_V1_BLOCKER_EVIDENCE_UNAVAILABLE",
    "PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON",
    "PLAIN_TEXT_PREFIX_V1_TRANSCRIPT_UNAVAILABLE",
    "PlainTextPrefixProof",
    "PlainTextPrefixProofStatus",
    "RecordedToolCall",
    "RunArtifactRecorder",
    "ThreadContextProvider",
    "ThreadConversation",
    "ThreadHistoryArtifactIdentity",
    "ThreadHistorySourceIdentity",
    "ThreadHistorySourceIntegrityError",
    "ThreadHistorySourceManifest",
    "ThreadHistorySourceScope",
    "ThreadHistoryHit",
    "ThreadHistorySearcher",
    "ThreadTurnContext",
]
