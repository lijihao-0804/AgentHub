"""The adapter that supplies thread history to the runtime.

It reads only finished turns, so the same run recomputes the same context after
a durable resume. There is no summarization, no embedding and no memory
extraction here on purpose: history is the earlier turns, verbatim and bounded,
and anything cleverer would be a new abstraction the runtime cannot explain.
M3 freezes the source scaffold of every replay. Its paged path never loads the
whole thread and reports an explicit lower-bound when work limits are reached.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from sqlalchemy import Text, and_, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.evidence_registry import (
    RAG_EVIDENCE_TOOL_IDENTITIES,
    visible_evidence_registry,
)
from packages.agent_runtime.history_transcript import transcript_from_payload
from packages.agent_runtime.models import AgentRun, RunStep
from packages.agent_runtime.work_layer import (
    HISTORY_SOURCE_ARTIFACT_LIMIT,
    HISTORY_SOURCE_COMPLETE,
    HISTORY_SOURCE_COUNT_VERSION,
    HISTORY_SOURCE_MAX_ARTIFACTS,
    HISTORY_SOURCE_MAX_PAGE_SIZE,
    HISTORY_SOURCE_MAX_TURNS,
    HISTORY_SOURCE_MAX_TURNS_SCANNED,
    HISTORY_SOURCE_SUMMARY_ELIGIBILITY_UNKNOWN,
    HISTORY_SOURCE_SUMMARY_REASON_NOT_CLASSIFIED,
    HISTORY_SOURCE_TURN_SCAN_LIMIT,
    HISTORY_SOURCE_TURN_SELECTION_LIMIT,
    PLAIN_TEXT_PREFIX_V1,
    PLAIN_TEXT_PREFIX_V1_BLOCKER_EVIDENCE_UNAVAILABLE,
    PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON,
    PLAIN_TEXT_PREFIX_V1_TRANSCRIPT_UNAVAILABLE,
    PlainTextPrefixProof,
    PlainTextPrefixProofStatus,
    ThreadConversation,
    ThreadHistoryArtifactIdentity,
    ThreadHistoryHit,
    ThreadHistorySourceIdentity,
    ThreadHistorySourceIncompleteError,
    ThreadHistorySourceIntegrityError,
    ThreadHistorySourceManifest,
    ThreadHistorySourceScope,
    ThreadTurnContext,
)
from packages.approvals.models import Approval
from packages.artifacts.models import Artifact
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.text import search_terms
from packages.threads.models import ThreadTurn

_SUCCEEDED = "SUCCEEDED"
_PLAIN_TEXT_PREFIX_ALLOWED_STEP_KINDS = frozenset(
    {
        "PREPARE",
        "MODEL",
        "GUARD",
        "FINISH",
    }
)
MAX_REF_TITLE = 80
SOURCE_ARTIFACT_PAGE_SIZE = 100
MAX_LEGACY_TURNS = 200
SOURCE_COUNT_PAGE_SIZE = 500
SOURCE_CONTENT_MAX_BYTES = 8 * 1024 * 1024

# How much of a matched turn is quoted back. Long enough to be an answer, short
# enough that five hits cannot swallow the context the search was supposed to
# save.
MAX_HIT_INPUT = 300
MAX_HIT_OUTPUT = 800
# The thread search shares the project-wide tokenizer; it is kept as a module
# level alias because "what counts as a term" is the whole reason this search
# works in Chinese, and a reader looking at the query builder should find it.
_terms = search_terms


def _excerpt(text_value: str, limit: int) -> str:
    text_value = (text_value or "").strip()
    if len(text_value) <= limit:
        return text_value
    return text_value[: limit - 1] + "…"


def _artifact_ref(artifact: Artifact) -> str:
    """One line standing in for a whole artifact.

    Twenty abstracts would eat the budget to tell the model something one line
    already tells it: that a search happened, and roughly what it found. A
    follow-up that really needs the contents searches again.
    """

    title = (artifact.title or "").strip()
    if len(title) > MAX_REF_TITLE:
        title = title[: MAX_REF_TITLE - 1] + "…"
    papers = artifact.content.get("papers") if isinstance(artifact.content, dict) else None
    count = len(papers) if isinstance(papers, list) else 0
    return f'[artifact: {artifact.type} "{title}" ({count} papers)]'


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact_content_sha256(artifact: Artifact) -> str:
    return canonical_json_hash(
        {
            "type": artifact.type,
            "title": artifact.title,
            "content": artifact.content,
        }
    )


def _source_revision_sha256(
    *,
    source_id: str,
    workspace_id: UUID,
    thread_id: UUID,
    turn_id: UUID,
    run_id: UUID,
    sequence: int,
    watermark: int,
    user_input_sha256: str,
    final_output_sha256: str,
    artifact_identities: tuple[ThreadHistoryArtifactIdentity, ...],
    transcript_sha256: str | None,
    evidence_registry_sha256: str | None,
    source_bindings: dict[str, object],
) -> str:
    return canonical_json_hash(
        {
            "source_id": source_id,
            "workspace_id": str(workspace_id),
            "thread_id": str(thread_id),
            "turn_id": str(turn_id),
            "run_id": str(run_id),
            "sequence": sequence,
            "watermark": watermark,
            "user_input_sha256": user_input_sha256,
            "final_output_sha256": final_output_sha256,
            "artifact_identities": [item.to_manifest() for item in artifact_identities],
            "transcript_sha256": transcript_sha256,
            "evidence_registry_sha256": evidence_registry_sha256,
            "source_bindings": source_bindings,
        }
    )


def _source_identity(
    turn: ThreadTurn,
    run: AgentRun,
    *,
    watermark: int,
    artifacts: tuple[Artifact, ...],
) -> ThreadHistorySourceIdentity:
    _complete_transcript(turn, run)
    source_id = f"thread-turn:{turn.id}"
    user_input_sha256 = _sha256_text(turn.user_input)
    final_output_sha256 = _sha256_text(run.final_output or "")
    artifact_identities = tuple(
        ThreadHistoryArtifactIdentity(
            artifact_id=artifact.id,
            content_sha256=_artifact_content_sha256(artifact),
        )
        for artifact in artifacts
    )
    return ThreadHistorySourceIdentity(
        source_id=source_id,
        workspace_id=turn.workspace_id,
        thread_id=turn.thread_id,
        turn_id=turn.id,
        run_id=run.id,
        sequence=turn.sequence,
        watermark=watermark,
        exchange_id=source_id,
        original_ref=source_id,
        count_version=HISTORY_SOURCE_COUNT_VERSION,
        user_input_sha256=user_input_sha256,
        final_output_sha256=final_output_sha256,
        revision_sha256=_source_revision_sha256(
            source_id=source_id,
            workspace_id=turn.workspace_id,
            thread_id=turn.thread_id,
            turn_id=turn.id,
            run_id=run.id,
            sequence=turn.sequence,
            watermark=watermark,
            user_input_sha256=user_input_sha256,
            final_output_sha256=final_output_sha256,
            artifact_identities=artifact_identities,
            transcript_sha256=(
                canonical_json_hash(run.thread_history_transcript)
                if run.thread_history_transcript is not None
                else None
            ),
            evidence_registry_sha256=(
                canonical_json_hash(run.thread_history_evidence_registry)
                if run.thread_history_evidence_registry is not None
                else None
            ),
            source_bindings={
                "knowledge": run.effective_knowledge_snapshots,
                "wiki": run.effective_wiki_publications,
            },
        ),
        artifact_identities=artifact_identities,
        summary_eligibility=HISTORY_SOURCE_SUMMARY_ELIGIBILITY_UNKNOWN,
        summary_eligibility_reason=HISTORY_SOURCE_SUMMARY_REASON_NOT_CLASSIFIED,
    )


def _complete_transcript(turn: ThreadTurn, run: AgentRun):
    payload = run.thread_history_transcript
    if payload is None:
        if (run.tool_call_count or 0) > 0:
            raise ThreadHistorySourceIncompleteError(
                "Historical tool exchanges do not have a complete source transcript."
            )
        return ()
    try:
        transcript = transcript_from_payload(payload)
        messages = transcript.model_messages
        if (
            transcript.run_id != run.id
            or transcript.turn_id != turn.id
            or messages[0].content != turn.user_input
            or messages[-1].content != run.final_output
            or sum(len(message.tool_calls) for message in messages) != (run.tool_call_count or 0)
        ):
            raise ValueError("transcript does not match its source")
        rag_calls = {
            call.provider_tool_call_id
            for message in messages
            for call in message.tool_calls
            if call.name in RAG_EVIDENCE_TOOL_IDENTITIES
        }
        if rag_calls:
            registry = getattr(run, "thread_history_evidence_registry", None)
            if not isinstance(registry, dict):
                raise ThreadHistorySourceIncompleteError(
                    "Historical RAG references are unavailable."
                )
            for message in messages:
                if message.role == "tool" and message.tool_call_id in rag_calls:
                    try:
                        payload = json.loads(message.content or "null")
                    except (TypeError, ValueError):
                        raise ThreadHistorySourceIncompleteError(
                            "Historical RAG evidence is invalid."
                        ) from None
                    if (
                        message.name not in RAG_EVIDENCE_TOOL_IDENTITIES
                        or not isinstance(payload, dict)
                        or (
                            payload.get("evidence_format") != "rag_view_v1"
                            and not (
                                payload.get("status") == "ERROR"
                                and payload.get("evidence_format") is None
                                and "results" not in payload
                            )
                        )
                    ):
                        raise ThreadHistorySourceIncompleteError(
                            "Historical RAG evidence is not compact."
                        )
            try:
                visible_evidence_registry(messages, registry)
            except AgentHubError:
                raise ThreadHistorySourceIncompleteError(
                    "Historical RAG references are invalid."
                ) from None
        for message in messages:
            if message.role != "tool":
                continue
            try:
                result = json.loads(message.content or "null")
            except (TypeError, ValueError):
                continue
            if (
                isinstance(result, dict)
                and result.get("evidence_format") != "rag_view_v1"
                and (
                    result.get("truncated") is True
                    or isinstance(result.get("data"), dict)
                    and result["data"].get("truncated") is True
                )
            ):
                raise ThreadHistorySourceIncompleteError(
                    "A historical tool result was truncated and cannot be reconstructed."
                )
        return messages
    except ThreadHistorySourceIncompleteError:
        raise
    except (TypeError, ValueError):
        raise ThreadHistorySourceIntegrityError("A stored thread transcript is invalid.") from None


def _visible_transcript(turn: ThreadTurn, run: AgentRun, artifacts: tuple[Artifact, ...]):
    messages = _complete_transcript(turn, run)
    if not messages or not artifacts:
        return messages
    final = messages[-1]
    return (
        *messages[:-1],
        replace(
            final,
            content="\n".join(
                (final.content or "", *(_artifact_ref(artifact) for artifact in artifacts))
            ),
        ),
    )


def _plain_text_prefix_v1_proof(
    turn: ThreadTurn,
    run: AgentRun,
    *,
    messages: tuple,
    artifacts: tuple[Artifact, ...],
    approval_run_ids: frozenset[UUID] | None,
    action_run_ids: frozenset[UUID] | None,
) -> PlainTextPrefixProof:
    """Classify a completed exchange from persisted server facts.

    A persisted transcript is required. Missing transcript data stays
    unproven even when the Run counters look clean; eligibility also requires
    independent persisted evidence that no tool, approval, action, evidence,
    or artifact participated in the exchange.
    """

    if approval_run_ids is None or action_run_ids is None:
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.UNPROVEN,
            PLAIN_TEXT_PREFIX_V1_BLOCKER_EVIDENCE_UNAVAILABLE,
        )
    if run.status != _SUCCEEDED:
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.UNPROVEN,
            "PLAIN_TEXT_PREFIX_V1_COMPLETION_STATUS_UNAVAILABLE",
        )
    if (
        isinstance(run.tool_call_count, bool)
        or not isinstance(run.tool_call_count, int)
        or run.tool_call_count < 0
    ):
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.UNPROVEN,
            "PLAIN_TEXT_PREFIX_V1_TOOL_COUNT_UNAVAILABLE",
        )
    if run.failure_code is not None:
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_FAILURE_OR_UNKNOWN_OUTCOME",
        )
    if run.tool_call_count:
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_TOOL_ACTIVITY",
        )
    if run.waiting_approval_id is not None or run.id in approval_run_ids:
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_APPROVAL_ACTIVITY",
        )
    if run.id in action_run_ids:
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_ACTION_ACTIVITY",
        )
    if run.thread_history_evidence_registry is not None:
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_EVIDENCE_PRESENT",
        )
    if run.effective_knowledge_snapshots or run.effective_wiki_publications:
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_SOURCE_EVIDENCE_PRESENT",
        )
    if artifacts:
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_ARTIFACT_PRESENT",
        )
    if not isinstance(messages, tuple) or not messages:
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.UNPROVEN,
            PLAIN_TEXT_PREFIX_V1_TRANSCRIPT_UNAVAILABLE,
        )
    if (
        not isinstance(turn.user_input, str)
        or not turn.user_input.strip()
        or not isinstance(run.final_output, str)
        or not run.final_output.strip()
        or "\x00" in turn.user_input
        or "\x00" in run.final_output
    ):
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_EXCHANGE_TEXT_INVALID",
        )
    if any(
        message.role not in {"user", "assistant"}
        or message.tool_calls
        or message.tool_call_id is not None
        or not isinstance(message.content, str)
        for message in messages
    ):
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_TRANSCRIPT_NOT_PLAIN_TEXT",
        )
    if (
        len(messages) != 2
        or messages[0].role != "user"
        or messages[0].content != turn.user_input
        or messages[1].role != "assistant"
        or messages[1].content != run.final_output
    ):
        return PlainTextPrefixProof(
            PlainTextPrefixProofStatus.INELIGIBLE,
            "PLAIN_TEXT_PREFIX_V1_TRANSCRIPT_NOT_SIMPLE_EXCHANGE",
        )
    return PlainTextPrefixProof(
        PlainTextPrefixProofStatus.ELIGIBLE,
        PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON,
    )


@dataclass(frozen=True, slots=True)
class _SourceScan:
    selected_keys: tuple[tuple[int, UUID], ...]
    observed_eligible_count: int
    count_complete: bool


@dataclass(frozen=True, slots=True)
class _ArtifactScan:
    artifact_ids: tuple[UUID, ...]
    overflow: bool


def _manifest_for(
    *,
    scope: ThreadHistorySourceScope,
    sources: tuple[ThreadHistorySourceIdentity, ...],
    available_eligible_source_count: int,
    available_source_count: int | None,
    available_source_count_scope: str,
    available_artifact_count: int | None,
    available_artifact_count_scope: str,
    provenance_complete: bool,
    below_watermark_has_eligible_source: bool = False,
    work_limit_reached: bool = False,
    limit_reason: str | None = None,
) -> ThreadHistorySourceManifest:
    if limit_reason == HISTORY_SOURCE_TURN_SCAN_LIMIT:
        detail = "The turn scan bound was reached before all sources were measured."
    elif limit_reason == HISTORY_SOURCE_TURN_SELECTION_LIMIT:
        detail = (
            "The measured source window exceeds max_turns; no subset is safe to "
            "dispatch without token accounting for the omitted history."
        )
    elif limit_reason == HISTORY_SOURCE_ARTIFACT_LIMIT:
        detail = "The artifact bound was reached before source provenance was complete."
    else:
        detail = None
    selected_artifact_count = sum(len(source.artifact_identities) for source in sources)
    return ThreadHistorySourceManifest(
        scope=scope,
        status=("LIMIT_REACHED" if limit_reason is not None else HISTORY_SOURCE_COMPLETE),
        selected_sources=sources,
        selected_source_count=len(sources),
        available_eligible_source_count=available_eligible_source_count,
        selected_artifact_count=selected_artifact_count,
        available_artifact_count=available_artifact_count,
        available_artifact_count_scope=available_artifact_count_scope,
        available_source_count=available_source_count,
        available_source_count_scope=available_source_count_scope,
        provenance_complete=provenance_complete,
        below_watermark_has_eligible_source=below_watermark_has_eligible_source,
        work_limit_reached=work_limit_reached,
        limit_reason=limit_reason,
        detail=detail,
    )


def _turn_key(turn: ThreadTurn) -> tuple[int, UUID]:
    return turn.sequence, turn.id


def _validate_summary_source_policy(value: str | None) -> None:
    if value not in {None, PLAIN_TEXT_PREFIX_V1}:
        raise ValueError(f"summary_source_policy must be {PLAIN_TEXT_PREFIX_V1} or None")


class SqlAlchemyThreadContextProvider:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def current_turn_sequence(
        self, *, workspace_id: UUID, thread_id: UUID, before_run_id: UUID
    ) -> int | None:
        """Public seam used by runtime tests and workers to prove the bound."""

        return await self.sequence_for_run(
            workspace_id=workspace_id, thread_id=thread_id, run_id=before_run_id
        )

    async def conversation_before_sequence(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        sequence: int,
        max_turns: int,
    ) -> ThreadConversation:
        """Pure sequence-bounded history, shared by the provider and tests."""

        return await self._legacy_conversation(
            workspace_id=workspace_id, thread_id=thread_id, sequence=sequence, max_turns=max_turns
        )

    async def search_before_sequence(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        sequence: int,
        query: str,
        limit: int,
    ) -> tuple[ThreadHistoryHit, ...]:
        return await self._search(
            workspace_id=workspace_id,
            thread_id=thread_id,
            sequence=sequence,
            query=query,
            limit=limit,
        )

    async def conversation(
        self, *, workspace_id: UUID, thread_id: UUID, before_run_id: UUID, max_turns: int
    ) -> ThreadConversation:
        if max_turns <= 0:
            return ThreadConversation()
        async with self.session_factory() as session:
            current_sequence = await self._current_sequence(
                session, workspace_id=workspace_id, thread_id=thread_id, run_id=before_run_id
            )
            if current_sequence is None:
                return ThreadConversation()
        return await self._legacy_conversation(
            workspace_id=workspace_id,
            thread_id=thread_id,
            sequence=current_sequence,
            max_turns=max_turns,
        )

    async def history_sources(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        before_run_id: UUID,
        max_turns: int,
        max_turns_scanned: int = HISTORY_SOURCE_MAX_TURNS_SCANNED,
        max_artifacts: int = HISTORY_SOURCE_MAX_ARTIFACTS,
        artifact_page_size: int = SOURCE_ARTIFACT_PAGE_SIZE,
        summary_source_policy: str | None = None,
    ) -> ThreadConversation:
        """Measure a fixed W1 prefix with paged reads and return its exact sources.

        W1 is the sequence currently linked to ``before_run_id``. W0 is an
        optional, already validated source manifest supplied to
        :meth:`replay_history_sources`; this method only starts at W1.
        """

        _validate_summary_source_policy(summary_source_policy)
        for value, name, maximum in (
            (max_turns, "max_turns", HISTORY_SOURCE_MAX_TURNS),
            (
                max_turns_scanned,
                "max_turns_scanned",
                HISTORY_SOURCE_MAX_TURNS_SCANNED,
            ),
            (max_artifacts, "max_artifacts", HISTORY_SOURCE_MAX_ARTIFACTS),
            (
                artifact_page_size,
                "artifact_page_size",
                HISTORY_SOURCE_MAX_PAGE_SIZE,
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if max_turns_scanned < max_turns:
            raise ValueError("max_turns_scanned cannot be below max_turns")
        async with self.session_factory() as session:
            await self._begin_frozen_read(session)
            current_sequence = await self._m3_current_sequence(
                session,
                workspace_id=workspace_id,
                thread_id=thread_id,
                run_id=before_run_id,
            )
            if current_sequence is None:
                return ThreadConversation()
            if current_sequence == 1:
                empty = ThreadHistorySourceManifest.empty(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    before_run_id=before_run_id,
                    before_sequence=current_sequence,
                    max_turns=max_turns,
                    max_turns_scanned=max_turns_scanned,
                    max_artifacts=max_artifacts,
                    artifact_page_size=artifact_page_size,
                )
                return ThreadConversation(source_manifest=empty)
            scope = ThreadHistorySourceScope(
                workspace_id=workspace_id,
                thread_id=thread_id,
                before_run_id=before_run_id,
                before_sequence=current_sequence,
                max_turns=max_turns,
                max_turns_scanned=max_turns_scanned,
                max_artifacts=max_artifacts,
                artifact_page_size=artifact_page_size,
            )
            binding_scan_complete = await self._assert_history_bindings_provable(
                session, scope=scope
            )
            scan = await self._scan_newest_sources(session, scope=scope)
            if not binding_scan_complete and scan.count_complete:
                scan = replace(scan, count_complete=False)
            return await self._materialize_sources(
                session,
                scope=scope,
                scan=scan,
                summary_source_policy=summary_source_policy,
            )

    async def replay_history_sources(
        self,
        *,
        manifest: Mapping[str, object],
        summary_source_policy: str | None = None,
    ) -> ThreadConversation:
        """Rebuild exactly one frozen source set, or fail closed.

        Replay reads only the source identities in the manifest. It does not
        re-measure the candidate set: an omitted Turn that succeeds later is
        outside the frozen selection and must not alter recovery. A selected
        Turn/Run or Artifact must still exist and match its frozen identity.
        """

        _validate_summary_source_policy(summary_source_policy)
        try:
            frozen = ThreadHistorySourceManifest.from_manifest(manifest)
        except (TypeError, ValueError) as error:
            raise ThreadHistorySourceIntegrityError(
                "The frozen thread history manifest is invalid."
            ) from error
        scope = frozen.scope
        async with self.session_factory() as session:
            await self._begin_frozen_read(session)
            current_sequence = await self._m3_current_sequence(
                session,
                workspace_id=scope.workspace_id,
                thread_id=scope.thread_id,
                run_id=scope.before_run_id,
            )
            if current_sequence != scope.before_sequence:
                raise ThreadHistorySourceIntegrityError(
                    "The frozen thread history watermark no longer matches its Run."
                )
            if scope.before_sequence == 1:
                if (
                    frozen.status != HISTORY_SOURCE_COMPLETE
                    or frozen.selected_sources
                    or frozen.available_source_count != 0
                    or frozen.available_eligible_source_count != 0
                    or frozen.available_artifact_count != 0
                ):
                    raise ThreadHistorySourceIntegrityError(
                        "The frozen empty history does not match its watermark."
                    )
                return ThreadConversation(source_manifest=frozen)
            baseline = await self._materialize_frozen_sources(
                session,
                scope=scope,
                manifest=frozen,
                sources=frozen.selected_sources,
                summary_source_policy=summary_source_policy,
            )
        return baseline

    @staticmethod
    async def _begin_frozen_read(session: AsyncSession) -> None:
        """Start the snapshot before the first source or watermark query."""

        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))

    @staticmethod
    async def _assert_history_bindings_provable(
        session: AsyncSession, *, scope: ThreadHistorySourceScope
    ) -> bool:
        turn_ids = list(
            await session.scalars(
                select(ThreadTurn.id)
                .where(
                    ThreadTurn.workspace_id == scope.workspace_id,
                    ThreadTurn.thread_id == scope.thread_id,
                    ThreadTurn.sequence < scope.before_sequence,
                )
                .order_by(ThreadTurn.sequence.desc(), ThreadTurn.id.desc())
                .limit(scope.max_turns_scanned + 1)
            )
        )
        complete = len(turn_ids) <= scope.max_turns_scanned
        turn_ids = turn_ids[: scope.max_turns_scanned]
        if not turn_ids:
            return complete
        inconsistent_success = (
            select(AgentRun.id)
            .where(
                AgentRun.workspace_id == ThreadTurn.workspace_id,
                AgentRun.status == _SUCCEEDED,
                or_(
                    AgentRun.thread_turn_id == ThreadTurn.id, AgentRun.id == ThreadTurn.agent_run_id
                ),
                or_(
                    AgentRun.thread_turn_id.is_distinct_from(ThreadTurn.id),
                    AgentRun.id.is_distinct_from(ThreadTurn.agent_run_id),
                    AgentRun.thread_id.is_distinct_from(ThreadTurn.thread_id),
                    AgentRun.input_text.is_distinct_from(ThreadTurn.user_input),
                    AgentRun.final_output.is_(None),
                ),
            )
            .exists()
        )
        invalid = await session.scalar(
            select(ThreadTurn.id)
            .where(
                ThreadTurn.workspace_id == scope.workspace_id,
                ThreadTurn.thread_id == scope.thread_id,
                ThreadTurn.id.in_(turn_ids),
                or_(ThreadTurn.legacy_recovery_required.is_(True), inconsistent_success),
            )
            .limit(1)
        )
        if invalid is not None:
            raise ThreadHistorySourceIncompleteError("A historical turn binding is not provable.")
        return complete

    async def _scan_newest_sources(
        self,
        session: AsyncSession,
        *,
        scope: ThreadHistorySourceScope,
    ) -> _SourceScan:
        """Count and retain by keys before loading any business row.

        One extra row is the sentinel: only its presence proves the scan bound
        was reached. Loading happens afterward, restricted to the selected
        window and the frozen watermark.
        """

        selected_keys, observed, count_complete = await self._page_source_keys(
            session,
            workspace_id=scope.workspace_id,
            thread_id=scope.thread_id,
            candidate_before=(scope.before_sequence, UUID(int=0)),
            page_size=min(scope.max_turns_scanned + 1, SOURCE_COUNT_PAGE_SIZE),
            max_rows=scope.max_turns_scanned + 1,
        )
        selected_count = min(len(selected_keys), scope.max_turns)
        return _SourceScan(
            selected_keys=selected_keys[:selected_count],
            observed_eligible_count=observed,
            count_complete=count_complete,
        )

    async def _page_source_keys(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        candidate_before: tuple[int, UUID],
        page_size: int,
        max_rows: int,
    ) -> tuple[tuple[tuple[int, UUID], ...], int, bool]:
        keys: list[tuple[int, UUID]] = []
        cursor = candidate_before
        while len(keys) < max_rows:
            remaining = max_rows - len(keys)
            statement = (
                self._source_candidates_statement(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    candidate_before=cursor,
                )
                .with_only_columns(ThreadTurn.sequence, ThreadTurn.id)
                .order_by(ThreadTurn.sequence.desc(), ThreadTurn.id.desc())
                .limit(min(page_size, remaining))
            )
            batch = list((await session.execute(statement)).all())
            keys.extend(batch)
            if len(batch) < min(page_size, remaining):
                return tuple(keys), len(keys), True
            last_sequence, last_id = batch[-1]
            cursor = (last_sequence, last_id)
        return tuple(keys), len(keys), False

    async def _load_source_rows(
        self,
        session: AsyncSession,
        *,
        scope: ThreadHistorySourceScope,
        keys: tuple[tuple[int, UUID], ...],
    ) -> list[tuple[ThreadTurn, AgentRun]]:
        if not keys:
            return []
        turn_ids = tuple(turn_id for _, turn_id in keys)
        await self._assert_source_content_bound(session, scope=scope, turn_ids=turn_ids)
        rows = list(
            (
                await session.execute(
                    self._source_candidates_statement(
                        workspace_id=scope.workspace_id,
                        thread_id=scope.thread_id,
                        candidate_before=(scope.before_sequence, UUID(int=0)),
                    )
                    .where(ThreadTurn.id.in_(turn_ids))
                    .order_by(ThreadTurn.sequence, ThreadTurn.id)
                )
            ).all()
        )
        actual_keys = tuple(_turn_key(turn) for turn, _ in rows)
        if actual_keys != tuple(sorted(keys)):
            raise ThreadHistorySourceIntegrityError(
                "The measured thread source identities changed before materialization."
            )
        return rows

    async def _assert_source_content_bound(
        self, session: AsyncSession, *, scope: ThreadHistorySourceScope, turn_ids: tuple[UUID, ...]
    ) -> None:
        if not turn_ids:
            return
        per_row = (
            func.octet_length(ThreadTurn.user_input)
            + func.octet_length(AgentRun.input_text)
            + func.octet_length(func.coalesce(AgentRun.final_output, ""))
            + func.coalesce(func.octet_length(cast(AgentRun.thread_history_transcript, Text)), 0)
            + func.coalesce(
                func.octet_length(cast(AgentRun.thread_history_evidence_registry, Text)), 0
            )
            + func.coalesce(
                func.octet_length(cast(AgentRun.effective_knowledge_snapshots, Text)), 0
            )
            + func.coalesce(func.octet_length(cast(AgentRun.effective_wiki_publications, Text)), 0)
            + func.coalesce(func.octet_length(cast(AgentRun.effective_memory_snapshot, Text)), 0)
            + func.coalesce(
                func.octet_length(cast(AgentRun.effective_thread_history_snapshot, Text)), 0
            )
        )
        total = await session.scalar(
            self._source_candidates_statement(
                workspace_id=scope.workspace_id,
                thread_id=scope.thread_id,
                candidate_before=(scope.before_sequence, UUID(int=0)),
            )
            .with_only_columns(func.coalesce(func.sum(per_row), 0))
            .where(ThreadTurn.id.in_(turn_ids))
        )
        if total is None or total > SOURCE_CONTENT_MAX_BYTES:
            raise ThreadHistorySourceIncompleteError(
                "History source content exceeds its work limit."
            )

    async def _scan_selected_artifacts(
        self,
        session: AsyncSession,
        *,
        scope: ThreadHistorySourceScope,
        run_ids: tuple[UUID, ...],
    ) -> _ArtifactScan:
        """Read artifact rows in keyset pages without silently dropping overflow."""

        if not run_ids:
            return _ArtifactScan((), False)
        bounded_ids: list[UUID] = []
        cursor: tuple[datetime, UUID] | None = None
        while len(bounded_ids) < scope.max_artifacts:
            remaining = scope.max_artifacts - len(bounded_ids)
            statement = select(Artifact.id, Artifact.created_at).where(
                Artifact.workspace_id == scope.workspace_id,
                Artifact.thread_id == scope.thread_id,
                Artifact.run_id.in_(run_ids),
            )
            if cursor is not None:
                created_at, artifact_id = cursor
                statement = statement.where(
                    or_(
                        Artifact.created_at > created_at,
                        and_(
                            Artifact.created_at == created_at,
                            Artifact.id > artifact_id,
                        ),
                    )
                )
            page = list(
                (
                    await session.execute(
                        statement.order_by(Artifact.created_at, Artifact.id).limit(
                            min(scope.artifact_page_size, remaining)
                        )
                    )
                ).all()
            )
            bounded_ids.extend(artifact_id for artifact_id, _ in page)
            if len(page) < min(scope.artifact_page_size, remaining):
                return _ArtifactScan(
                    tuple(sorted(bounded_ids)),
                    False,
                )
            cursor = (page[-1].created_at, page[-1].id)
        overflow = await session.scalar(
            select(Artifact.id)
            .where(
                Artifact.workspace_id == scope.workspace_id,
                Artifact.thread_id == scope.thread_id,
                Artifact.run_id.in_(run_ids),
                or_(
                    Artifact.created_at > cursor[0],
                    and_(
                        Artifact.created_at == cursor[0],
                        Artifact.id > cursor[1],
                    ),
                ),
            )
            .limit(1)
        )
        return _ArtifactScan(tuple(sorted(bounded_ids)), overflow is not None)

    @staticmethod
    async def _assert_artifact_content_bound(
        session: AsyncSession, *, scope: ThreadHistorySourceScope, artifact_ids: tuple[UUID, ...]
    ) -> None:
        if not artifact_ids:
            return
        total = await session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        func.octet_length(func.coalesce(Artifact.title, ""))
                        + func.coalesce(func.octet_length(cast(Artifact.content, Text)), 0)
                    ),
                    0,
                )
            ).where(
                Artifact.workspace_id == scope.workspace_id,
                Artifact.thread_id == scope.thread_id,
                Artifact.id.in_(artifact_ids),
            )
        )
        if total is None or total > SOURCE_CONTENT_MAX_BYTES:
            raise ThreadHistorySourceIncompleteError("History artifacts exceed their work limit.")

    @staticmethod
    async def _plain_text_prefix_blockers(
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_ids: tuple[UUID, ...],
        enabled: bool,
    ) -> tuple[frozenset[UUID] | None, frozenset[UUID] | None]:
        """Read the opt-in persisted facts, or return fail-closed absence."""

        if not enabled:
            return None, None
        if not run_ids:
            return frozenset(), frozenset()
        try:
            async with session.begin_nested():
                approval_run_ids = frozenset(
                    await session.scalars(
                        select(Approval.run_id).where(
                            Approval.workspace_id == workspace_id,
                            Approval.run_id.in_(run_ids),
                        )
                    )
                )
                action_run_ids = frozenset(
                    await session.scalars(
                        select(RunStep.agent_run_id).where(
                            RunStep.workspace_id == workspace_id,
                            RunStep.agent_run_id.in_(run_ids),
                            RunStep.kind.not_in(_PLAIN_TEXT_PREFIX_ALLOWED_STEP_KINDS),
                        )
                    )
                )
        except Exception:
            # Historical compatibility and safety evidence are independent:
            # if the opt-in proof tables are unavailable, the source remains
            # exact instead of becoming silently summarizable.
            return None, None
        return approval_run_ids, action_run_ids

    async def _materialize_sources(
        self,
        session: AsyncSession,
        *,
        scope: ThreadHistorySourceScope,
        scan: _SourceScan,
        summary_source_policy: str | None = None,
    ) -> ThreadConversation:
        if scan.count_complete and scan.observed_eligible_count > scope.max_turns:
            measured_source_count = scan.observed_eligible_count
            manifest = _manifest_for(
                scope=scope,
                sources=(),
                available_eligible_source_count=measured_source_count,
                available_source_count=measured_source_count,
                available_source_count_scope="FROZEN_WATERMARK",
                available_artifact_count=0,
                available_artifact_count_scope="FROZEN_WATERMARK",
                provenance_complete=True,
                below_watermark_has_eligible_source=True,
                work_limit_reached=False,
                limit_reason=HISTORY_SOURCE_TURN_SELECTION_LIMIT,
            )
            return ThreadConversation(
                turns=(),
                turns_available=measured_source_count,
                turns_available_is_exact=True,
                source_manifest=manifest,
            )
        rows = await self._load_source_rows(
            session,
            scope=scope,
            keys=scan.selected_keys,
        )
        rows.sort(key=lambda item: _turn_key(item[0]))
        run_ids = tuple(run.id for _, run in rows)
        artifact_scan = await self._scan_selected_artifacts(
            session,
            scope=scope,
            run_ids=run_ids,
        )
        measured_source_count = len(scan.selected_keys)
        below_watermark = not scan.count_complete or (
            scan.observed_eligible_count > len(scan.selected_keys)
        )
        if artifact_scan.overflow:
            # One incomplete source would make the entire exchange identity
            # ambiguous. Return no source/turn rows and let the explicit limit
            # state tell the planner why the frozen window is unavailable.
            if scan.count_complete:
                limit_reason = HISTORY_SOURCE_ARTIFACT_LIMIT
            else:
                limit_reason = HISTORY_SOURCE_TURN_SCAN_LIMIT
            measured_source_count = (
                scan.observed_eligible_count if scan.count_complete else len(scan.selected_keys)
            )
            manifest = _manifest_for(
                scope=scope,
                sources=(),
                available_eligible_source_count=measured_source_count,
                available_source_count=(None if not scan.count_complete else measured_source_count),
                available_source_count_scope=(
                    "LOWER_BOUND" if not scan.count_complete else "FROZEN_WATERMARK"
                ),
                available_artifact_count=scope.max_artifacts + 1,
                available_artifact_count_scope="LOWER_BOUND",
                provenance_complete=False,
                below_watermark_has_eligible_source=below_watermark,
                work_limit_reached=not scan.count_complete,
                limit_reason=limit_reason,
            )
            return ThreadConversation(
                turns=(),
                turns_available=measured_source_count,
                turns_available_is_exact=scan.count_complete,
                source_manifest=manifest,
            )
        artifact_by_run: dict[UUID, dict[UUID, Artifact]] = {run_id: {} for run_id in run_ids}
        if artifact_scan.artifact_ids:
            await self._assert_artifact_content_bound(
                session, scope=scope, artifact_ids=artifact_scan.artifact_ids
            )
            artifacts = list(
                await session.scalars(
                    select(Artifact).where(
                        Artifact.workspace_id == scope.workspace_id,
                        Artifact.thread_id == scope.thread_id,
                        Artifact.run_id.in_(run_ids),
                        Artifact.id.in_(artifact_scan.artifact_ids),
                    )
                )
            )
            expected_ids = set(artifact_scan.artifact_ids)
            if {artifact.id for artifact in artifacts} != expected_ids:
                raise ThreadHistorySourceIntegrityError(
                    "A measured artifact identity disappeared during materialization."
                )
            for artifact in artifacts:
                if artifact.run_id not in artifact_by_run:
                    raise ThreadHistorySourceIntegrityError(
                        "An artifact escaped the selected source runs."
                    )
                artifact_by_run[artifact.run_id][artifact.id] = artifact
        rows_artifacts: dict[UUID, tuple[Artifact, ...]] = {}
        for run_id, values in artifact_by_run.items():
            rows_artifacts[run_id] = tuple(
                sorted(
                    values.values(),
                    key=lambda item: item.id,
                )
            )
        selected_artifact_count = len(artifact_scan.artifact_ids)
        approval_run_ids, action_run_ids = await self._plain_text_prefix_blockers(
            session,
            workspace_id=scope.workspace_id,
            run_ids=run_ids,
            enabled=summary_source_policy is not None,
        )
        if not scan.count_complete:
            limit_reason = HISTORY_SOURCE_TURN_SCAN_LIMIT
        else:
            limit_reason = None
        sources = tuple(
            _source_identity(
                turn,
                run,
                watermark=scope.before_sequence,
                artifacts=rows_artifacts.get(run.id, ()),
            )
            for turn, run in rows
        )
        manifest = _manifest_for(
            scope=scope,
            sources=sources,
            available_eligible_source_count=measured_source_count,
            available_source_count=(None if not scan.count_complete else measured_source_count),
            available_source_count_scope=(
                "LOWER_BOUND" if not scan.count_complete else "FROZEN_WATERMARK"
            ),
            available_artifact_count=selected_artifact_count,
            available_artifact_count_scope="FROZEN_WATERMARK",
            provenance_complete=True,
            below_watermark_has_eligible_source=below_watermark,
            work_limit_reached=not scan.count_complete,
            limit_reason=limit_reason,
        )
        turns = tuple(
            ThreadTurnContext(
                user_input=turn.user_input,
                final_output=run.final_output or "",
                turn_id=turn.id,
                run_id=run.id,
                sequence=turn.sequence,
                artifact_ids=tuple(artifact.id for artifact in rows_artifacts.get(run.id, ())),
                artifact_refs=tuple(
                    _artifact_ref(artifact) for artifact in rows_artifacts.get(run.id, ())
                ),
                messages=_visible_transcript(turn, run, rows_artifacts.get(run.id, ())),
                evidence_registry=run.thread_history_evidence_registry,
                source_knowledge_snapshots=tuple(run.effective_knowledge_snapshots or ()),
                source_wiki_publications=tuple(run.effective_wiki_publications or ()),
                plain_text_prefix_v1_proof=(
                    _plain_text_prefix_v1_proof(
                        turn,
                        run,
                        messages=_visible_transcript(
                            turn, run, rows_artifacts.get(run.id, ())
                        ),
                        artifacts=rows_artifacts.get(run.id, ()),
                        approval_run_ids=approval_run_ids,
                        action_run_ids=action_run_ids,
                    )
                    if summary_source_policy is not None
                    else None
                ),
            )
            for turn, run in rows
        )
        return ThreadConversation(
            turns=turns,
            turns_available=measured_source_count,
            turns_available_is_exact=scan.count_complete,
            source_manifest=manifest,
        )

    async def _materialize_frozen_sources(
        self,
        session: AsyncSession,
        *,
        scope: ThreadHistorySourceScope,
        manifest: ThreadHistorySourceManifest,
        sources: tuple[ThreadHistorySourceIdentity, ...],
        summary_source_policy: str | None = None,
    ) -> ThreadConversation:
        """Load only the manifest's selected IDs and verify every frozen identity."""

        if not sources:
            return ThreadConversation(
                turns=(),
                turns_available=(
                    manifest.available_source_count
                    if manifest.available_source_count is not None
                    else manifest.available_eligible_source_count
                ),
                turns_available_is_exact=manifest.available_source_count is not None,
                source_manifest=manifest,
            )

        turn_ids = tuple(source.turn_id for source in sources)
        await self._assert_source_content_bound(session, scope=scope, turn_ids=turn_ids)
        rows = list(
            (
                await session.execute(
                    self._source_candidates_statement(
                        workspace_id=scope.workspace_id,
                        thread_id=scope.thread_id,
                        candidate_before=(scope.before_sequence, UUID(int=0)),
                    )
                    .where(ThreadTurn.id.in_(turn_ids))
                    .order_by(ThreadTurn.sequence, ThreadTurn.id)
                )
            ).all()
        )
        rows_by_turn = {turn.id: (turn, run) for turn, run in rows}
        if set(rows_by_turn) != set(turn_ids):
            raise ThreadHistorySourceIntegrityError(
                "A frozen Turn or Run is missing or no longer eligible."
            )

        artifact_ids = tuple(
            artifact.artifact_id for source in sources for artifact in source.artifact_identities
        )
        artifacts = {}
        if artifact_ids:
            await self._assert_artifact_content_bound(
                session, scope=scope, artifact_ids=artifact_ids
            )
            artifacts = {
                artifact.id: artifact
                for artifact in await session.scalars(
                    select(Artifact).where(
                        Artifact.workspace_id == scope.workspace_id,
                        Artifact.thread_id == scope.thread_id,
                        Artifact.id.in_(artifact_ids),
                    )
                )
            }
            if set(artifacts) != set(artifact_ids):
                raise ThreadHistorySourceIntegrityError("A frozen Artifact identity is missing.")

        approval_run_ids, action_run_ids = await self._plain_text_prefix_blockers(
            session,
            workspace_id=scope.workspace_id,
            run_ids=tuple(source.run_id for source in sources),
            enabled=summary_source_policy is not None,
        )

        turns: list[ThreadTurnContext] = []
        for frozen in sources:
            turn, run = rows_by_turn[frozen.turn_id]
            run_artifacts = tuple(
                artifacts[identity.artifact_id] for identity in frozen.artifact_identities
            )
            if any(artifact.run_id != frozen.run_id for artifact in run_artifacts):
                raise ThreadHistorySourceIntegrityError(
                    "A frozen Artifact escaped its selected source Run."
                )
            rebuilt = _source_identity(
                turn,
                run,
                watermark=scope.before_sequence,
                artifacts=run_artifacts,
            )
            if rebuilt != frozen:
                raise ThreadHistorySourceIntegrityError("A frozen source identity has changed.")
            turns.append(
                ThreadTurnContext(
                    user_input=turn.user_input,
                    final_output=run.final_output or "",
                    turn_id=turn.id,
                    run_id=run.id,
                    sequence=turn.sequence,
                    artifact_ids=tuple(artifact.id for artifact in run_artifacts),
                    artifact_refs=tuple(_artifact_ref(artifact) for artifact in run_artifacts),
                    messages=_visible_transcript(turn, run, run_artifacts),
                    evidence_registry=run.thread_history_evidence_registry,
                    source_knowledge_snapshots=tuple(run.effective_knowledge_snapshots or ()),
                    source_wiki_publications=tuple(run.effective_wiki_publications or ()),
                    plain_text_prefix_v1_proof=(
                        _plain_text_prefix_v1_proof(
                            turn,
                            run,
                            messages=_visible_transcript(turn, run, run_artifacts),
                            artifacts=run_artifacts,
                            approval_run_ids=approval_run_ids,
                            action_run_ids=action_run_ids,
                        )
                        if summary_source_policy is not None
                        else None
                    ),
                )
            )

        return ThreadConversation(
            turns=tuple(turns),
            turns_available=(
                manifest.available_source_count
                if manifest.available_source_count is not None
                else manifest.available_eligible_source_count
            ),
            turns_available_is_exact=manifest.available_source_count is not None,
            source_manifest=manifest,
        )

    @staticmethod
    def _source_candidates_statement(
        *,
        workspace_id: UUID,
        thread_id: UUID,
        candidate_before: tuple[int, UUID],
    ):
        sequence, turn_id = candidate_before
        return (
            select(ThreadTurn, AgentRun)
            .join(
                AgentRun,
                (AgentRun.workspace_id == ThreadTurn.workspace_id)
                & (AgentRun.thread_turn_id == ThreadTurn.id)
                & (AgentRun.thread_id == ThreadTurn.thread_id),
            )
            .where(
                ThreadTurn.workspace_id == workspace_id,
                ThreadTurn.thread_id == thread_id,
                or_(
                    ThreadTurn.sequence < sequence,
                    and_(ThreadTurn.sequence == sequence, ThreadTurn.id < turn_id),
                ),
                AgentRun.status == _SUCCEEDED,
                AgentRun.final_output.is_not(None),
                AgentRun.input_text == ThreadTurn.user_input,
                ThreadTurn.agent_run_id == AgentRun.id,
                ThreadTurn.legacy_recovery_required.is_(False),
            )
        )

    async def _legacy_conversation(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        sequence: int,
        max_turns: int,
    ) -> ThreadConversation:
        if max_turns <= 0:
            return ThreadConversation()
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(ThreadTurn, AgentRun)
                    .join(
                        AgentRun,
                        (AgentRun.workspace_id == ThreadTurn.workspace_id)
                        & (AgentRun.thread_turn_id == ThreadTurn.id)
                        & (AgentRun.thread_id == ThreadTurn.thread_id),
                    )
                    .where(
                        ThreadTurn.workspace_id == workspace_id,
                        ThreadTurn.thread_id == thread_id,
                        ThreadTurn.sequence < sequence,
                        AgentRun.status == _SUCCEEDED,
                        AgentRun.final_output.is_not(None),
                        AgentRun.input_text == ThreadTurn.user_input,
                        ThreadTurn.agent_run_id == AgentRun.id,
                        ThreadTurn.legacy_recovery_required.is_(False),
                    )
                    .order_by(ThreadTurn.sequence)
                )
            ).all()
            available = len(rows)
            selected = rows[-max_turns:]
            run_ids = [run.id for _, run in selected]
            refs: dict[UUID, list[str]] = {}
            if run_ids:
                artifacts = await session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.workspace_id == workspace_id,
                        Artifact.thread_id == thread_id,
                        Artifact.run_id.in_(run_ids),
                    )
                    .order_by(Artifact.created_at, Artifact.id)
                )
                for artifact in artifacts:
                    if artifact.run_id is not None:
                        refs.setdefault(artifact.run_id, []).append(_artifact_ref(artifact))
            turns = tuple(
                ThreadTurnContext(
                    user_input=turn.user_input,
                    final_output=run.final_output or "",
                    artifact_refs=tuple(refs.get(run.id, ())),
                )
                for turn, run in selected
            )
        return ThreadConversation(turns=turns, turns_available=available)

    async def search(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        before_run_id: UUID,
        query: str,
        limit: int,
    ) -> tuple[ThreadHistoryHit, ...]:
        """Find earlier turns of *this* thread that mention the query terms.

        The scope is deliberately narrow: one thread, one workspace, finished
        turns only, and never the run doing the asking. A tool that could reach
        into a neighbouring thread would be a retrieval feature wearing a
        memory costume, and would need the governance that comes with one.
        """

        terms = _terms(query)
        if not terms or limit <= 0:
            return ()
        async with self.session_factory() as session:
            current_sequence = await self._current_sequence(
                session, workspace_id=workspace_id, thread_id=thread_id, run_id=before_run_id
            )
            if current_sequence is None:
                return ()
        return await self._search(
            workspace_id=workspace_id,
            thread_id=thread_id,
            sequence=current_sequence,
            query=query,
            limit=limit,
        )

    async def _search(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        sequence: int,
        query: str,
        limit: int,
    ) -> tuple[ThreadHistoryHit, ...]:
        terms = _terms(query)
        if not terms or limit <= 0:
            return ()
        conditions = [
            or_(
                ThreadTurn.user_input.ilike(f"%{term}%"),
                AgentRun.final_output.ilike(f"%{term}%"),
            )
            for term in terms
        ]
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(ThreadTurn, AgentRun)
                    .join(
                        AgentRun,
                        (AgentRun.workspace_id == ThreadTurn.workspace_id)
                        & (AgentRun.thread_turn_id == ThreadTurn.id)
                        & (AgentRun.thread_id == ThreadTurn.thread_id),
                    )
                    .where(
                        ThreadTurn.workspace_id == workspace_id,
                        ThreadTurn.thread_id == thread_id,
                        ThreadTurn.sequence < sequence,
                        AgentRun.status == _SUCCEEDED,
                        AgentRun.final_output.is_not(None),
                        AgentRun.input_text == ThreadTurn.user_input,
                        ThreadTurn.agent_run_id == AgentRun.id,
                        ThreadTurn.legacy_recovery_required.is_(False),
                        or_(*conditions),
                    )
                    .order_by(ThreadTurn.sequence)
                )
            ).all()
        scored: list[tuple[int, int, ThreadHistoryHit]] = []
        for turn, run in rows:
            haystack = f"{turn.user_input}\n{run.final_output or ''}".lower()
            matched = tuple(term for term in terms if term in haystack)
            if not matched:
                continue
            scored.append(
                (
                    len(matched),
                    turn.sequence,
                    ThreadHistoryHit(
                        sequence=turn.sequence,
                        user_input=_excerpt(turn.user_input, MAX_HIT_INPUT),
                        final_output=_excerpt(run.final_output or "", MAX_HIT_OUTPUT),
                        matched_terms=matched,
                    ),
                )
            )
        # Most terms matched wins; ties go to the more recent turn, because in a
        # conversation the later statement is the one that survived revision.
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return tuple(hit for _, _, hit in scored[:limit])

    async def sequence_for_run(
        self, *, workspace_id: UUID, thread_id: UUID, run_id: UUID
    ) -> int | None:
        """Return the linked Turn sequence, or ``None`` when no proof exists."""

        async with self.session_factory() as session:
            return await self._current_sequence(
                session, workspace_id=workspace_id, thread_id=thread_id, run_id=run_id
            )

    async def _current_sequence(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        run_id: UUID,
    ) -> int | None:
        """Resolve the immutable Turn sequence for the asking Run.

        The Run-to-Turn link is the authority. If an older Run has no such
        link, returns ``None``; history must fail closed rather than fall back
        to an unbounded time/order guess.
        """

        return await session.scalar(
            select(ThreadTurn.sequence)
            .join(
                AgentRun,
                (AgentRun.workspace_id == ThreadTurn.workspace_id)
                & (AgentRun.thread_turn_id == ThreadTurn.id),
            )
            .where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.thread_id == thread_id,
                AgentRun.id == run_id,
                ThreadTurn.workspace_id == workspace_id,
                ThreadTurn.thread_id == thread_id,
                AgentRun.input_text == ThreadTurn.user_input,
                ThreadTurn.agent_run_id == AgentRun.id,
                ThreadTurn.legacy_recovery_required.is_(False),
            )
        )

    async def _m3_current_sequence(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        run_id: UUID,
    ) -> int | None:
        """Resolve the immutable Turn sequence for the asking Run.

        The Run-to-Turn link is the authority. If an older Run has no such
        link, returns ``None``; history must fail closed rather than fall back
        to an unbounded time/order guess.
        """

        return await session.scalar(
            select(ThreadTurn.sequence)
            .join(
                AgentRun,
                (AgentRun.workspace_id == ThreadTurn.workspace_id)
                & (AgentRun.thread_turn_id == ThreadTurn.id),
            )
            .where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.thread_id == thread_id,
                AgentRun.id == run_id,
                ThreadTurn.workspace_id == workspace_id,
                ThreadTurn.thread_id == thread_id,
                AgentRun.input_text == ThreadTurn.user_input,
                ThreadTurn.agent_run_id == AgentRun.id,
                ThreadTurn.legacy_recovery_required.is_(False),
            )
        )


__all__ = ["SqlAlchemyThreadContextProvider"]
