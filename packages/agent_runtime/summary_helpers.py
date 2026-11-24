"""Pure validation, integrity, and pricing helpers extracted from summary_service.py."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy.exc import DBAPIError

from packages.agent_runtime.frozen import (
    SUMMARY_NON_THINKING_MODE,
    SUMMARY_NON_THINKING_MODEL,
    SUMMARY_NON_THINKING_PROVIDER,
    frozen_summary_request_identity,
)
from packages.agent_runtime.history_checkpoint import (
    PersistedCheckpointBinding,
    VerifiedCheckpointBinding,
)
from packages.agent_runtime.summary_contracts import (
    MAX_RESPONSE_BYTES,
    MAX_SOURCES_PER_JOB,
    MAX_SUMMARY_MODEL_ROUND,
    SUMMARY_ARTIFACT_VERSION,
    SUMMARY_JOB_IDENTITY_VERSION,
    SUMMARY_SOURCE_INDEX_VERSION,
    NewSummaryJob,
    NewSummarySource,
    PublishedSummaryRevision,
    SummaryJobBinding,
    SummaryJobKey,
    SummaryResponseData,
    SummaryServiceError,
    SummarySettlement,
    SummaryTrustDecision,
    SummaryVerifierPhase,
)
from packages.agent_runtime.summary_models import (
    ThreadSummaryArtifact,
    ThreadSummaryArtifactStatus,
    ThreadSummaryDispatch,
    ThreadSummaryJob,
    ThreadSummaryJobSource,
    ThreadSummaryResponse,
)
from packages.agent_runtime.summary_prompt import (
    SummaryInputContext,
    build_summary_prompt,
)
from packages.agent_runtime.summary_state import (
    SummaryJobStatus,
    SummaryState,
    recover_expired_lease,
)
from packages.agent_runtime.summary_validation import (
    SummaryValidationResult,
)
from packages.agent_runtime.work_layer import PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON
from packages.core.canonical.json_hash import canonical_json, canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.models import PricingSnapshot

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _state(job: ThreadSummaryJob) -> SummaryState:
    return SummaryState(
        status=SummaryJobStatus(job.status),
        lease_token=job.lease_token,
        budget_reserved=job.budget_reserved,
        response_ref=str(job.response_id) if job.response_id is not None else None,
        revision_ref=str(job.artifact_id) if job.artifact_id is not None else None,
    )


def _apply_state(job: ThreadSummaryJob, state: SummaryState) -> None:
    job.status = state.status.value
    job.lease_token = state.lease_token
    job.budget_reserved = state.budget_reserved


def _settlement(
    job: ThreadSummaryJob,
    *,
    validation: SummaryValidationResult | None = None,
) -> SummarySettlement:
    validation_result = validation.as_dict() if validation is not None else job.validation_result
    return SummarySettlement(
        key=SummaryJobKey(job.workspace_id, job.thread_id, job.id),
        status=job.status,
        response_id=job.response_id,
        artifact_id=job.artifact_id,
        validation_result=validation_result,
    )


def _boundary_conflict(existing: ThreadSummaryJob) -> SummaryServiceError:
    return SummaryServiceError(
        "SUMMARY_DISPATCH_BOUNDARY_CONFLICT",
        (
            f"Summary job {existing.id} already owns trigger Run "
            f"{existing.trigger_run_id} model round {existing.model_round} "
            f"with identity {existing.identity_hash}."
        ),
    )


def _require_model_round_boundary(job: ThreadSummaryJob) -> None:
    if not _positive_int(job.model_round) or job.model_round > MAX_SUMMARY_MODEL_ROUND:
        raise SummaryServiceError(
            "SUMMARY_DISPATCH_BOUNDARY_REQUIRED",
            "A legacy summary job without a model-round boundary cannot dispatch.",
        )
    checkpoint = _checkpoint_anchor(job)
    if job.model_round != checkpoint.checkpoint_model_round_count + 1:
        raise SummaryServiceError(
            "SUMMARY_DISPATCH_BOUNDARY_INVALID",
            "The summary model round does not follow its fixed checkpoint.",
        )


def _checkpoint_anchor(job: ThreadSummaryJob) -> PersistedCheckpointBinding:
    checkpoint_id = getattr(job, "checkpoint_id", None)
    checkpoint_ns = getattr(job, "checkpoint_ns", None)
    checkpoint_model_round_count = getattr(job, "checkpoint_model_round_count", None)
    checkpoint_binding_sha256 = getattr(job, "checkpoint_binding_sha256", None)
    if (
        not isinstance(checkpoint_id, str)
        or not checkpoint_id
        or not isinstance(checkpoint_ns, str)
        or not _non_negative_int(checkpoint_model_round_count)
        or checkpoint_model_round_count >= MAX_SUMMARY_MODEL_ROUND
        or not _sha256(checkpoint_binding_sha256)
    ):
        raise SummaryServiceError(
            "SUMMARY_DISPATCH_BOUNDARY_REQUIRED",
            "The summary job has no complete fixed LangGraph checkpoint identity.",
        )
    return PersistedCheckpointBinding(
        workspace_id=job.workspace_id,
        thread_id=job.thread_id,
        run_id=job.trigger_run_id,
        checkpoint_id=checkpoint_id,
        checkpoint_ns=checkpoint_ns,
        checkpoint_model_round_count=checkpoint_model_round_count,
        checkpoint_binding_sha256=checkpoint_binding_sha256,
    )


def _trusted_summary_model_round(
    command: NewSummaryJob,
    checkpoint: VerifiedCheckpointBinding,
) -> int:
    model_round = checkpoint.checkpoint_model_round_count + 1
    if model_round > MAX_SUMMARY_MODEL_ROUND:
        raise SummaryServiceError(
            "SUMMARY_CHECKPOINT_ROUND_INVALID",
            "The checkpoint has no room for another model round.",
        )
    if command.model_round is not None and command.model_round != model_round:
        raise SummaryServiceError(
            "SUMMARY_CHECKPOINT_ROUND_MISMATCH",
            "The asserted model round does not follow the exact checkpoint.",
        )
    return model_round


def _binding(job: ThreadSummaryJob, phase: SummaryVerifierPhase) -> SummaryJobBinding:
    return SummaryJobBinding(
        workspace_id=job.workspace_id,
        thread_id=job.thread_id,
        job_id=job.id,
        trigger_run_id=job.trigger_run_id,
        model_round=job.model_round,
        created_by=job.created_by,
        identity_hash=job.identity_hash,
        source_watermark=job.source_watermark,
        source_scope=dict(job.source_scope),
        source_scope_hash=job.source_scope_hash,
        source_content_hash=job.source_content_hash,
        parent_artifact_id=job.parent_artifact_id,
        parent_revision_hash=job.parent_revision_hash,
        policy_version=job.policy_version,
        prompt_version=job.prompt_version,
        model_spec_hash=job.model_spec_hash,
        permission_snapshot_hash=job.permission_snapshot_hash,
        response_id=job.response_id,
        artifact_id=job.artifact_id,
        phase=phase,
        checkpoint_id=getattr(job, "checkpoint_id", None),
        checkpoint_ns=getattr(job, "checkpoint_ns", None),
        checkpoint_model_round_count=getattr(job, "checkpoint_model_round_count", None),
        checkpoint_binding_sha256=getattr(job, "checkpoint_binding_sha256", None),
    )


def _summary_identity_hash(
    command: NewSummaryJob,
    *,
    checkpoint: VerifiedCheckpointBinding,
    model_round: int,
) -> str:
    _validate_new_job(command)
    return canonical_json_hash(
        {
            "version": SUMMARY_JOB_IDENTITY_VERSION,
            "workspace_id": str(command.workspace_id),
            "thread_id": str(command.thread_id),
            "trigger_run_id": str(command.trigger_run_id),
            "model_round": model_round,
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_ns": checkpoint.checkpoint_ns,
            "checkpoint_model_round_count": (checkpoint.checkpoint_model_round_count),
            "checkpoint_binding_sha256": checkpoint.checkpoint_binding_sha256,
            "source_watermark": command.source_watermark,
            "source_scope": dict(command.source_scope),
            "source_content_hash": _source_content_hash(command),
            "parent_artifact_id": (
                str(command.parent_artifact_id) if command.parent_artifact_id is not None else None
            ),
            "parent_revision_hash": command.parent_revision_hash,
            "policy_spec": dict(command.policy_spec),
            "policy_version": command.policy_version,
            "prompt_version": command.prompt_version,
            "budget_version": command.budget_version,
            "model_spec": dict(command.model_spec),
            "tokenizer_id": command.tokenizer_id,
            "tokenizer_version": command.tokenizer_version,
            "target_budget_tokens": command.target_budget_tokens,
            "permission_snapshot_hash": command.permission_snapshot_hash,
        }
    )


def _source_content_hash(command: NewSummaryJob) -> str:
    return canonical_json_hash(
        {
            "version": "thread_summary_source_content_v1",
            "sources": [
                {
                    "source_id": source.source_id,
                    "turn_id": str(source.turn_id),
                    "run_id": str(source.run_id),
                    "sequence": source.sequence,
                    "original_ref": source.original_ref,
                    "source_identity_hash": source.source_identity_hash,
                    "revision_sha256": source.revision_sha256,
                    "user_input_sha256": source.user_input_sha256,
                    "final_output_sha256": source.final_output_sha256,
                    "artifact_identities": [dict(item) for item in source.artifact_identities],
                    "summary_eligibility": source.summary_eligibility,
                    "eligibility_reason": source.eligibility_reason,
                }
                for source in sorted(command.sources, key=lambda item: item.sequence)
            ],
        }
    )


def _validate_new_job(command: NewSummaryJob) -> None:
    for name in ("workspace_id", "thread_id", "trigger_run_id", "created_by"):
        if not isinstance(getattr(command, name), UUID):
            raise TypeError(f"{name} must be a UUID")
    if (command.parent_artifact_id is None) != (command.parent_revision_hash is None):
        raise SummaryServiceError(
            "SUMMARY_PARENT_INVALID",
            "Parent artifact and revision hash must be supplied together.",
        )
    if not _positive_int(command.source_watermark):
        raise ValueError("source_watermark must be a positive integer")
    if not _positive_int(command.target_budget_tokens):
        raise ValueError("target_budget_tokens must be a positive integer")
    if not 1 <= len(command.sources) <= MAX_SOURCES_PER_JOB:
        raise SummaryServiceError(
            "SUMMARY_SOURCE_COUNT_INVALID",
            f"A summary job requires 1-{MAX_SOURCES_PER_JOB} sources.",
        )
    sequences = [source.sequence for source in command.sources]
    source_ids = [source.source_id for source in command.sources]
    if len(set(sequences)) != len(sequences) or len(set(source_ids)) != len(source_ids):
        raise SummaryServiceError(
            "SUMMARY_SOURCE_DUPLICATE",
            "Source IDs and sequences must be unique.",
        )
    if sequences != sorted(sequences):
        raise SummaryServiceError(
            "SUMMARY_SOURCE_ORDER_INVALID",
            "Sources must be supplied in ascending sequence order.",
        )
    if any(
        not _positive_int(sequence) or sequence >= command.source_watermark
        for sequence in sequences
    ):
        raise SummaryServiceError(
            "SUMMARY_SOURCE_OUT_OF_SCOPE",
            "Every source must be before the frozen watermark.",
        )
    for source in command.sources:
        for name in ("turn_id", "run_id"):
            if not isinstance(getattr(source, name), UUID):
                raise TypeError(f"source {name} must be a UUID")
        if source.summary_eligibility not in {"ELIGIBLE", "INELIGIBLE", "UNKNOWN"}:
            raise SummaryServiceError(
                "SUMMARY_SOURCE_ELIGIBILITY_INVALID",
                "Source summary eligibility is invalid.",
            )
        for value in (
            source.source_identity_hash,
            source.revision_sha256,
            source.user_input_sha256,
            source.final_output_sha256,
        ):
            if not _sha256(value):
                raise SummaryServiceError(
                    "SUMMARY_SOURCE_HASH_INVALID",
                    "A source hash is not lowercase SHA-256.",
                )
    for value in (command.permission_snapshot_hash, command.parent_revision_hash):
        if value is not None and not _sha256(value):
            raise SummaryServiceError(
                "SUMMARY_JOB_HASH_INVALID",
                "A job binding hash is not lowercase SHA-256.",
            )
    for name in (
        "policy_version",
        "prompt_version",
        "budget_version",
        "tokenizer_id",
        "tokenizer_version",
    ):
        value = getattr(command, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    try:
        canonical_json_hash(dict(command.source_scope))
        canonical_json_hash(dict(command.policy_spec))
        canonical_json_hash(dict(command.model_spec))
    except (TypeError, ValueError) as error:
        raise SummaryServiceError(
            "SUMMARY_JOB_JSON_INVALID",
            "Job metadata must be canonical JSON.",
        ) from error
    if not isinstance(command.checkpoint_id, str) or not command.checkpoint_id:
        raise ValueError("checkpoint_id must be a non-empty string")
    if not isinstance(command.checkpoint_ns, str):
        raise ValueError("checkpoint_ns must be a string")
    try:
        checkpoint_id_bytes = len(command.checkpoint_id.encode("utf-8"))
        checkpoint_ns_bytes = len(command.checkpoint_ns.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("checkpoint selector must be valid UTF-8 text") from None
    if checkpoint_id_bytes > 1024 or checkpoint_ns_bytes > 1024:
        raise ValueError("checkpoint selectors must not exceed 1024 UTF-8 bytes")
    if command.model_round is not None and (
        not _positive_int(command.model_round) or command.model_round > MAX_SUMMARY_MODEL_ROUND
    ):
        raise ValueError(
            f"model_round must be a positive integer no greater than {MAX_SUMMARY_MODEL_ROUND}"
        )


def _response_bound_failure(
    response: ThreadSummaryResponse, dispatch: ThreadSummaryDispatch
) -> str | None:
    """Keep observed cost, but never promote usage outside the reservation."""
    if response.usage_metadata.get("cost_status") == "INVALID_REPORTED":
        return "SUMMARY_PROVIDER_COST_UNVERIFIED"
    if response.input_tokens is None or response.output_tokens is None:
        return "SUMMARY_PROVIDER_USAGE_UNVERIFIED"
    if (
        response.input_tokens > dispatch.reserved_input_tokens
        or response.output_tokens > dispatch.reserved_output_tokens
    ):
        return "SUMMARY_PROVIDER_USAGE_EXCEEDS_RESERVATION"
    if response.cost_amount is not None:
        if response.cost_currency != dispatch.reserved_currency:
            return "SUMMARY_PROVIDER_COST_CURRENCY_MISMATCH"
        if (
            dispatch.reserved_cost_amount is None
            or response.cost_amount > dispatch.reserved_cost_amount
        ):
            return "SUMMARY_PROVIDER_COST_EXCEEDS_RESERVATION"
    return None


def _validate_response_data(response: SummaryResponseData) -> None:
    if not isinstance(response, SummaryResponseData):
        raise TypeError("response must be SummaryResponseData")
    if not isinstance(response.provider, str) or not response.provider.strip():
        raise ValueError("provider must be a non-empty string")
    if not isinstance(response.model, str) or not response.model.strip():
        raise ValueError("model must be a non-empty string")
    usage_values = (
        response.input_tokens,
        response.output_tokens,
        response.total_tokens,
    )
    if any(value is not None for value in usage_values):
        if any(not _non_negative_int(value) for value in usage_values):
            raise SummaryServiceError(
                "SUMMARY_RESPONSE_USAGE_INVALID",
                "Input, output and total tokens must all be present.",
            )
        assert response.input_tokens is not None
        assert response.output_tokens is not None
        assert response.total_tokens is not None
        if response.total_tokens != response.input_tokens + response.output_tokens:
            raise SummaryServiceError(
                "SUMMARY_RESPONSE_USAGE_INVALID",
                "Total tokens must equal input plus output tokens.",
            )
    if response.cached_tokens is not None and not _non_negative_int(response.cached_tokens):
        raise SummaryServiceError(
            "SUMMARY_RESPONSE_USAGE_INVALID",
            "Cached tokens must be non-negative.",
        )
    cost_values = (
        response.cost_amount,
        response.cost_currency,
        response.cost_is_estimate,
    )
    if any(value is not None for value in cost_values) and any(
        value is None for value in cost_values
    ):
        raise SummaryServiceError(
            "SUMMARY_RESPONSE_COST_INVALID",
            "Response cost amount, currency and estimate flag must be supplied together.",
        )
    if response.cost_amount is not None and response.cost_amount < 0:
        raise SummaryServiceError(
            "SUMMARY_RESPONSE_COST_INVALID",
            "Response cost must be non-negative.",
        )
    if response.cost_currency is not None and not _currency(response.cost_currency):
        raise SummaryServiceError(
            "SUMMARY_RESPONSE_COST_INVALID",
            "Response currency must be a three-letter code.",
        )


def _encode_raw_response(
    value: Mapping[str, Any] | str | bytes,
) -> tuple[dict[str, str], str]:
    """Store provider bytes without parsing, including malformed JSON and NULs."""
    if isinstance(value, Mapping):
        raw = canonical_json(dict(value)).encode("utf-8")
    elif isinstance(value, str):
        raw = value.encode("utf-8", errors="strict")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise TypeError("raw_response must be a mapping, string, or bytes")
    envelope = {"format": "summary_raw_base64_v1", "data": base64.b64encode(raw).decode("ascii")}
    if len(json.dumps(envelope).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("encoded response exceeds the byte limit")
    return envelope, hashlib.sha256(raw).hexdigest()


def _decode_raw_response(response: ThreadSummaryResponse) -> bytes:
    envelope = response.raw_response
    try:
        if (
            isinstance(envelope, dict)
            and set(envelope) == {"format", "data"}
            and envelope["format"] == "summary_raw_base64_v1"
            and isinstance(envelope["data"], str)
        ):
            raw = base64.b64decode(envelope["data"], validate=True)
        elif isinstance(envelope, dict):
            # Compatibility for isolated pre-release rows stored as parsed JSON.
            raw = canonical_json(envelope).encode("utf-8")
        else:
            raise ValueError("stored response envelope is invalid")
        if hashlib.sha256(raw).hexdigest() != response.response_sha256:
            raise ValueError("stored response identity differs")
        return raw
    except (ValueError, TypeError, UnicodeError, binascii.Error) as error:
        raise SummaryServiceError(
            "SUMMARY_RESPONSE_INVARIANT", "The persisted provider response is invalid."
        ) from error


def _context_matches_job(
    context: SummaryInputContext,
    job: ThreadSummaryJob,
    sources: Sequence[ThreadSummaryJobSource],
) -> bool:
    if (
        context.workspace_id != str(job.workspace_id)
        or context.thread_id != str(job.thread_id)
        or context.watermark != job.source_watermark
        or not _permission_hash_matches(context.permission_snapshot, job.permission_snapshot_hash)
        or len(context.sources) != len(sources)
        or context.prefix_first_sequence != sources[0].sequence
        or context.prefix_last_sequence != sources[-1].sequence
    ):
        return False
    for prompt_source, stored in zip(context.sources, sources, strict=True):
        if (
            prompt_source.source_id != stored.source_id
            or prompt_source.workspace_id != str(stored.workspace_id)
            or prompt_source.thread_id != str(stored.thread_id)
            or prompt_source.identity != stored.source_identity_hash
            or prompt_source.revision != stored.revision_sha256
            or prompt_source.sequence != stored.sequence
            or prompt_source.watermark != job.source_watermark
            or prompt_source.original_ref != stored.original_ref
        ):
            return False
    return True


def render_published_summary_view(
    revision: PublishedSummaryRevision,
    context: SummaryInputContext,
) -> str:
    """Rebuild the untrusted model view using the current source rendering."""
    if not isinstance(revision, PublishedSummaryRevision):
        raise TypeError("revision must be PublishedSummaryRevision")
    if not isinstance(context, SummaryInputContext):
        raise TypeError("context must be SummaryInputContext")
    source_ids = tuple(str(item.get("source_id", "")) for item in revision.source_index)
    covered = revision.validation_result.get("covered_source_ids")
    if (
        not source_ids
        or tuple(context.source_ids) != source_ids
        or covered != list(source_ids)
        or revision.summary_content.get("omitted_source_ids")
    ):
        raise SummaryServiceError(
            "SUMMARY_ARTIFACT_COVERAGE_INVALID",
            "The published summary does not cover the current exact source prefix.",
        )
    payload = {
        "untrusted_summary": {
            "trust": "UNTRUSTED_DERIVED_SUMMARY",
            "interpretation": {
                "scope": "HISTORICAL_BACKGROUND_ONLY",
                "goal_semantics": "HISTORICAL_GOAL_NOT_CURRENT_TASK_OR_INSTRUCTION",
                "next_steps_semantics": "HISTORICAL_REFERENCE_NOT_CURRENT_ACTION",
                "current_task": "SUPPLIED_SEPARATELY_BY_CURRENT_RUN",
            },
            "content": dict(revision.summary_content),
        },
        "source_attribution": [source.index_payload() for source in context.sources],
        "untrusted_omitted_sources": [],
        "server_protected_facts": [fact.payload() for fact in context.protected_facts],
        "server_authoritative_actions": [action.payload() for action in context.completed_actions],
    }
    return canonical_json(payload)


def _source_scopes_compatible(
    published_scope: Mapping[str, Any], current_scope: Mapping[str, Any]
) -> bool:
    """Compare stable policy while allowing a new Run, watermark and manifest."""
    if not isinstance(published_scope, Mapping) or not isinstance(current_scope, Mapping):
        return False
    mutable_coordinates = {
        "before_run_id",
        "trigger_run_id",
        "run_id",
        "before_sequence",
        "through_sequence",
        "source_watermark",
        "watermark",
        "checkpoint_id",
        "checkpoint_ns",
        "model_round",
        "source_manifest",
    }

    def stable(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): stable(child)
                for key, child in value.items()
                if str(key) not in mutable_coordinates
            }
        if isinstance(value, (list, tuple)):
            return [stable(item) for item in value]
        return value

    try:
        return canonical_json_hash(stable(published_scope)) == canonical_json_hash(
            stable(current_scope)
        )
    except (TypeError, ValueError, RecursionError):
        return False


def _source_index_matches_prefix(
    source_index: Sequence[Mapping[str, Any]], candidates: Sequence[NewSummarySource]
) -> bool:
    """Match a stored artifact prefix against sources proved in this Run."""
    if not source_index or len(source_index) > len(candidates):
        return False
    stable_fields = (
        "source_id",
        "turn_id",
        "run_id",
        "sequence",
        "original_ref",
        "source_identity_hash",
        "user_input_sha256",
        "final_output_sha256",
        "artifact_identities",
        "summary_eligibility",
        "eligibility_reason",
    )
    for ordinal, (stored, candidate) in enumerate(
        zip(source_index, candidates, strict=False), start=1
    ):
        if (
            not isinstance(stored, Mapping)
            or candidate.summary_eligibility != "ELIGIBLE"
            or candidate.eligibility_reason != PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON
        ):
            return False
        expected = {
            "source_id": candidate.source_id,
            "turn_id": str(candidate.turn_id),
            "run_id": str(candidate.run_id),
            "sequence": candidate.sequence,
            "original_ref": candidate.original_ref,
            "source_identity_hash": candidate.source_identity_hash,
            "user_input_sha256": candidate.user_input_sha256,
            "final_output_sha256": candidate.final_output_sha256,
            "artifact_identities": [dict(item) for item in candidate.artifact_identities],
            "summary_eligibility": candidate.summary_eligibility,
            "eligibility_reason": candidate.eligibility_reason,
        }
        if stored.get("ordinal") != ordinal or any(
            stored.get(field) != expected[field] for field in stable_fields
        ):
            return False
    return True


def _artifact_is_integrity_valid(
    artifact: ThreadSummaryArtifact,
    job: ThreadSummaryJob,
    response: ThreadSummaryResponse,
    sources: Sequence[ThreadSummaryJobSource],
) -> bool:
    """Recompute the published artifact's immutable source/content hash chain."""
    try:
        if (
            artifact.workspace_id != job.workspace_id
            or artifact.thread_id != job.thread_id
            or artifact.job_id != job.id
            or artifact.response_id != response.id
            or job.response_id != response.id
            or job.artifact_id != artifact.id
            or job.dispatch_id is None
            or response.dispatch_id != job.dispatch_id
            or response.job_id != job.id
            or response.provider != job.model_spec.get("provider")
            or response.model != job.model_spec.get("model")
            or job.status != SummaryJobStatus.PUBLISHED.value
            or artifact.publication_status != ThreadSummaryArtifactStatus.PUBLISHED.value
            or not isinstance(job.source_scope, Mapping)
            or canonical_json_hash(dict(job.source_scope)) != job.source_scope_hash
            or canonical_json_hash(dict(job.model_spec)) != job.model_spec_hash
            or not _sha256(job.identity_hash)
            or not _sha256(response.response_sha256)
            or not isinstance(artifact.summary_content, Mapping)
            or not isinstance(artifact.source_index, list)
            or not isinstance(artifact.validation_result, Mapping)
            or artifact.validation_result.get("valid") is not True
            or artifact.validation_result.get("validator_version") != artifact.validator_version
            or job.validation_result != artifact.validation_result
        ):
            return False
        _decode_raw_response(response)
        expected_index = [_source_index_entry(source) for source in sources]
        if not sources or artifact.source_index != expected_index:
            return False
        source_content_hash = canonical_json_hash(
            {
                "version": "thread_summary_source_content_v1",
                "sources": [
                    {
                        "source_id": source.source_id,
                        "turn_id": str(source.turn_id),
                        "run_id": str(source.run_id),
                        "sequence": source.sequence,
                        "original_ref": source.original_ref,
                        "source_identity_hash": source.source_identity_hash,
                        "revision_sha256": source.revision_sha256,
                        "user_input_sha256": source.user_input_sha256,
                        "final_output_sha256": source.final_output_sha256,
                        "artifact_identities": [dict(item) for item in source.artifact_identities],
                        "summary_eligibility": source.summary_eligibility,
                        "eligibility_reason": source.eligibility_reason,
                    }
                    for source in sorted(sources, key=lambda item: item.sequence)
                ],
            }
        )
        if source_content_hash != job.source_content_hash:
            return False
        source_ids = [source.source_id for source in sources]
        if (
            artifact.validation_result.get("covered_source_ids") != source_ids
            or artifact.summary_content.get("status") != "OK"
            or artifact.summary_content.get("omitted_source_ids")
        ):
            return False
        content_hash = canonical_json_hash(dict(artifact.summary_content))
        source_index_hash = canonical_json_hash(
            {"version": SUMMARY_SOURCE_INDEX_VERSION, "sources": expected_index}
        )
        summary_identity_hash = canonical_json_hash(
            {
                "version": SUMMARY_ARTIFACT_VERSION,
                "job_identity_hash": job.identity_hash,
                "content_hash": content_hash,
            }
        )
        revision_hash = canonical_json_hash(
            {
                "version": SUMMARY_ARTIFACT_VERSION,
                "workspace_id": str(job.workspace_id),
                "thread_id": str(job.thread_id),
                "job_identity_hash": job.identity_hash,
                "response_sha256": response.response_sha256,
                "parent_artifact_id": (
                    str(job.parent_artifact_id) if job.parent_artifact_id is not None else None
                ),
                "parent_revision_hash": job.parent_revision_hash,
                "revision_number": artifact.revision_number,
                "content_hash": content_hash,
                "source_index_hash": source_index_hash,
                "validator_version": artifact.validator_version,
            }
        )
        return (
            artifact.content_hash == content_hash
            and artifact.source_index_hash == source_index_hash
            and artifact.summary_identity_hash == summary_identity_hash
            and artifact.revision_hash == revision_hash
        )
    except (AttributeError, TypeError, ValueError, RecursionError, SummaryServiceError):
        return False


def _verify_job_source_scope(
    key: SummaryJobKey,
    job: ThreadSummaryJob,
    *,
    sources: Sequence[ThreadSummaryJobSource],
) -> None:
    if not sources:
        raise SummaryServiceError(
            "SUMMARY_SOURCE_INVARIANT",
            "The immutable source manifest is empty.",
        )
    if len(sources) != len({source.source_id for source in sources}) or len(sources) != len(
        {source.sequence for source in sources}
    ):
        raise SummaryServiceError(
            "SUMMARY_SOURCE_INVARIANT",
            "The immutable source manifest contains duplicate identities.",
        )
    previous_sequence = 0
    for source in sources:
        if (
            source.workspace_id != key.workspace_id
            or source.thread_id != key.thread_id
            or source.job_id != key.job_id
            or source.sequence <= previous_sequence
            or source.sequence >= job.source_watermark
        ):
            raise SummaryServiceError(
                "SUMMARY_SOURCE_INVARIANT",
                "The source manifest escapes the job tenant or watermark.",
            )
        if not (
            _sha256(source.source_identity_hash)
            and _sha256(source.revision_sha256)
            and _sha256(source.user_input_sha256)
            and _sha256(source.final_output_sha256)
        ):
            raise SummaryServiceError(
                "SUMMARY_SOURCE_INVARIANT",
                "A source manifest hash is malformed.",
            )
        previous_sequence = source.sequence


def _permission_hash_matches(snapshot: str, expected_hash: str) -> bool:
    return snapshot == expected_hash or hashlib.sha256(snapshot.encode("utf-8")).hexdigest() == (
        expected_hash
    )


def _source_index_entry(source: ThreadSummaryJobSource) -> dict[str, Any]:
    return {
        "workspace_id": str(source.workspace_id),
        "thread_id": str(source.thread_id),
        "ordinal": source.ordinal,
        "source_id": source.source_id,
        "turn_id": str(source.turn_id),
        "run_id": str(source.run_id),
        "sequence": source.sequence,
        "original_ref": source.original_ref,
        "source_identity_hash": source.source_identity_hash,
        "revision_sha256": source.revision_sha256,
        "user_input_sha256": source.user_input_sha256,
        "final_output_sha256": source.final_output_sha256,
        "artifact_identities": list(source.artifact_identities),
        "summary_eligibility": source.summary_eligibility,
        "eligibility_reason": source.eligibility_reason,
    }


def _summary_request_mode(job: ThreadSummaryJob) -> str:
    """Recover a request mode only when policy and model identities agree."""
    policy = getattr(job, "policy_spec", None)
    model_spec = getattr(job, "model_spec", None)
    if not isinstance(policy, Mapping) or not isinstance(model_spec, Mapping):
        return ""
    try:
        identity = frozen_summary_request_identity(policy)
    except (AgentHubError, TypeError, ValueError):
        return ""
    request_mode = identity["request_mode"]
    if request_mode == SUMMARY_NON_THINKING_MODE:
        if (
            model_spec.get("provider") != SUMMARY_NON_THINKING_PROVIDER
            or model_spec.get("model") != SUMMARY_NON_THINKING_MODEL
            or any(model_spec.get(key) != value for key, value in identity.items())
        ):
            return ""
    elif any(
        key in model_spec
        for key in identity
        if key not in {"request_strategy_version", "request_mode"}
    ):
        return ""
    return request_mode


def _publish_request_strategy_error(job: ThreadSummaryJob) -> SummaryServiceError | None:
    """Require an explicitly supported request strategy at final publication."""
    request_mode = _summary_request_mode(job)
    if request_mode == SUMMARY_NON_THINKING_MODE:
        return SummaryServiceError(
            "SUMMARY_REQUEST_COUNTER_UNAVAILABLE",
            "A validated summary cannot be published without its frozen full-request counter.",
        )
    if request_mode != "provider_default":
        return SummaryServiceError(
            "SUMMARY_REQUEST_STRATEGY_UNSUPPORTED",
            "The frozen summary request strategy is not supported for publication.",
        )
    return None


def _verified_request_spec(
    job: ThreadSummaryJob,
    *,
    context: SummaryInputContext,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    request_mode = _summary_request_mode(job)
    if not request_mode:
        raise SummaryServiceError(
            "SUMMARY_REQUEST_STRATEGY_UNSUPPORTED",
            "The summary request mode is not bound by a supported frozen strategy.",
        )
    prompt = build_summary_prompt(context, prompt_version=job.prompt_version)
    prompt_hash = canonical_json_hash(
        {
            "system": prompt.system_prompt,
            "user": prompt.user_prompt,
            "prompt_version": prompt.prompt_version,
            "input_version": prompt.input_version,
            "output_schema_version": prompt.output_schema_version,
        }
    )
    payload = {
        "event": "SUMMARY_DISPATCH",
        "job_id": str(job.id),
        "identity_hash": job.identity_hash,
        "model_round": job.model_round,
        "source_watermark": job.source_watermark,
        "source_scope_hash": job.source_scope_hash,
        "source_content_hash": job.source_content_hash,
        "permission_snapshot_hash": job.permission_snapshot_hash,
        "model_spec_hash": job.model_spec_hash,
        "prompt_version": job.prompt_version,
        "prompt_hash": prompt_hash,
        "reserved_input_tokens": input_tokens,
        "reserved_output_tokens": output_tokens,
    }
    if "request_strategy_version" in job.policy_spec:
        identity = frozen_summary_request_identity(job.policy_spec)
        if identity["request_mode"] != request_mode:
            raise SummaryServiceError(
                "SUMMARY_REQUEST_STRATEGY_UNSUPPORTED",
                "The frozen dispatch request identity is inconsistent.",
            )
        payload.update(identity)
    return payload


def _reservation_cost(
    pricing: PricingSnapshot,
    *,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    input_price = _price_component(pricing.input_price_per_1m, "input_price_per_1m")
    output_price = _price_component(pricing.output_price_per_1m, "output_price_per_1m")
    cached_value = getattr(pricing, "cached_input_price_per_1m", None)
    cached_input_price = (
        _price_component(cached_value, "cached_input_price_per_1m")
        if cached_value is not None
        else None
    )
    effective_input_price = (
        input_price if cached_input_price is None else max(input_price, cached_input_price)
    )
    if not _currency(pricing.currency):
        raise ValueError("invalid reservation currency")
    amount = (
        Decimal(input_tokens) / Decimal(1_000_000) * effective_input_price
        + Decimal(output_tokens) / Decimal(1_000_000) * output_price
    ).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid reservation cost")
    return amount


def _price_component(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a non-negative finite decimal")
    try:
        component = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a non-negative finite decimal") from error
    if not component.is_finite() or component < 0:
        raise ValueError(f"{field} must be a non-negative finite decimal")
    return component


def _pricing_snapshot_content_hash(snapshot: PricingSnapshot) -> str:
    """Recompute the pricing identity without importing evaluation package services."""

    def decimal_text(value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")

    return canonical_json_hash(
        {
            "name": snapshot.name,
            "provider": snapshot.provider,
            "model": snapshot.model,
            "currency": snapshot.currency,
            "input_price_per_1m": decimal_text(snapshot.input_price_per_1m),
            "output_price_per_1m": decimal_text(snapshot.output_price_per_1m),
            "cached_input_price_per_1m": decimal_text(snapshot.cached_input_price_per_1m),
            "effective_at": snapshot.effective_at.isoformat(),
            "source_note": snapshot.source_note,
        }
    )


def _mark_unknown(job: ThreadSummaryJob, now: datetime) -> None:
    state = recover_expired_lease(_state(job))
    _apply_state(job, state)
    job.lease_token = None
    job.lease_expires_at = None
    job.failure_code = "SUMMARY_DISPATCH_OUTCOME_UNKNOWN"
    job.completed_at = now


def _mark_stale(job: ThreadSummaryJob, now: datetime, code: str) -> None:
    job.status = SummaryJobStatus.STALE.value
    job.lease_token = None
    job.lease_expires_at = None
    job.failure_code = code
    job.completed_at = now


def _release_lease(job: ThreadSummaryJob) -> None:
    job.lease_token = None
    job.lease_expires_at = None


def _owns_live_lease(
    job: ThreadSummaryJob,
    lease_token: str,
    now: datetime,
) -> bool:
    return (
        isinstance(lease_token, str)
        and bool(lease_token)
        and job.lease_token == lease_token
        and _is_live(job.lease_expires_at, now)
    )


def _is_live(expiry: datetime | None, now: datetime) -> bool:
    if expiry is None:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry.astimezone(UTC) > now


def _failure_code(decision: SummaryTrustDecision, fallback: str) -> str:
    reasons = tuple(reason for reason in decision.reason_codes if reason.isidentifier())
    if not reasons:
        return fallback
    value = reasons[0].upper()
    return value[:96]


def _constraint_name(error: DBAPIError) -> str | None:
    diag = getattr(error.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _currency(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 3
        and all("A" <= character <= "Z" for character in value)
    )


