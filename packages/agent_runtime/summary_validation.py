"""Strict mechanical validation for one untrusted M4 summary candidate.

Validation is intentionally evidence-bound.  It can prove that a candidate
matches the frozen schema, cites only sources in the complete prefix, retains
server-owned protected material, preserves declared references and action
states, and fits an injected token budget.  It cannot prove that arbitrary
free-form prose is semantically faithful to every source statement.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from jsonschema import Draft202012Validator, SchemaError

from packages.agent_runtime.summary_prompt import (
    SummaryActionStatus,
    SummaryInputContext,
    SummaryInputContextError,
    summary_output_schema,
    validate_summary_input_context,
)
from packages.core.canonical.json_hash import canonical_json

SUMMARY_VALIDATOR_VERSION: Final = "summary_mechanical_v1"
MAX_CANDIDATE_BYTES: Final = 4 * 1024 * 1024
MAX_CANDIDATE_DEPTH: Final = 32
MAX_CANDIDATE_NODES: Final = 100_000
MAX_ITEM_CHARS: Final = 1_000_000

_TOP_LEVEL_KEYS = frozenset(
    {
        "status",
        "goal",
        "decisions",
        "supported_facts",
        "conflicts",
        "open_questions",
        "completed_actions_refs",
        "next_steps",
        "omitted_source_ids",
    }
)
_TEXT_COLLECTIONS = (
    "decisions",
    "supported_facts",
    "conflicts",
    "open_questions",
    "next_steps",
)


class SummaryValidationCode(StrEnum):
    """Stable machine-readable failure reasons."""

    INPUT_INVALID = "INPUT_INVALID"
    CANDIDATE_MALFORMED = "CANDIDATE_MALFORMED"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    SOURCE_UNKNOWN = "SOURCE_UNKNOWN"
    SOURCE_OUT_OF_SCOPE = "SOURCE_OUT_OF_SCOPE"
    SOURCE_RANGE_UNCOVERED = "SOURCE_RANGE_UNCOVERED"
    COVERAGE_INVALID = "COVERAGE_INVALID"
    PROTECTED_FACT_MISSING = "PROTECTED_FACT_MISSING"
    REFERENCE_MISSING = "REFERENCE_MISSING"
    ACTION_REFERENCE_INVALID = "ACTION_REFERENCE_INVALID"
    ACTION_STATUS_INVALID = "ACTION_STATUS_INVALID"
    INSUFFICIENT_BUDGET = "INSUFFICIENT_BUDGET"
    SUMMARY_BUDGET_EXCEEDED = "SUMMARY_BUDGET_EXCEEDED"
    FINAL_VIEW_BUDGET_EXCEEDED = "FINAL_VIEW_BUDGET_EXCEEDED"


class SummaryTokenCounter(Protocol):
    """Injected deterministic counter for serialized prompt/output/view text."""

    def count(self, text: str) -> int:
        """Return a non-negative token count."""


@dataclass(frozen=True, slots=True)
class SummaryValidationIssue:
    code: SummaryValidationCode
    path: str
    source_ids: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, SummaryValidationCode):
            raise TypeError("issue code must be SummaryValidationCode")
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("issue path must be a non-empty string")
        source_ids = tuple(self.source_ids)
        if any(not isinstance(source_id, str) or not source_id for source_id in source_ids):
            raise ValueError("issue source_ids must be non-empty strings")
        object.__setattr__(self, "source_ids", source_ids)
        if not isinstance(self.detail, str):
            raise TypeError("issue detail must be a string")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "path": self.path,
            "source_ids": list(self.source_ids),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SummaryTextItem:
    text: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SummaryActionItem:
    action_ref: str
    status: SummaryActionStatus
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValidatedSummary:
    """Canonical candidate content and deterministic verification evidence."""

    status: str
    goal: SummaryTextItem
    decisions: tuple[SummaryTextItem, ...]
    supported_facts: tuple[SummaryTextItem, ...]
    conflicts: tuple[SummaryTextItem, ...]
    open_questions: tuple[SummaryTextItem, ...]
    completed_actions_refs: tuple[SummaryActionItem, ...]
    next_steps: tuple[SummaryTextItem, ...]
    omitted_source_ids: tuple[str, ...]
    canonical_content: str
    summary_estimated_tokens: int
    final_view_text: str
    final_view_estimated_tokens: int
    covered_source_ids: tuple[str, ...]
    validator_version: str = SUMMARY_VALIDATOR_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Return a detached structured artifact, excluding validation notes."""

        return {
            "status": self.status,
            "goal": _text_item_payload(self.goal),
            "decisions": [_text_item_payload(item) for item in self.decisions],
            "supported_facts": [_text_item_payload(item) for item in self.supported_facts],
            "conflicts": [_text_item_payload(item) for item in self.conflicts],
            "open_questions": [_text_item_payload(item) for item in self.open_questions],
            "completed_actions_refs": [
                {
                    "action_ref": item.action_ref,
                    "status": item.status.value,
                    "source_ids": list(item.source_ids),
                }
                for item in self.completed_actions_refs
            ],
            "next_steps": [_text_item_payload(item) for item in self.next_steps],
            "omitted_source_ids": list(self.omitted_source_ids),
        }


@dataclass(frozen=True, slots=True)
class SummaryValidationResult:
    valid: bool
    validator_version: str
    issues: tuple[SummaryValidationIssue, ...]
    candidate: ValidatedSummary | None
    summary_estimated_tokens: int
    final_view_estimated_tokens: int
    covered_source_ids: tuple[str, ...]
    omitted_source_ids: tuple[str, ...]

    @property
    def status(self) -> str:
        return "VALID" if self.valid else "INVALID"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "valid": self.valid,
            "validator_version": self.validator_version,
            "issues": [issue.as_dict() for issue in self.issues],
            "summary_estimated_tokens": self.summary_estimated_tokens,
            "final_view_estimated_tokens": self.final_view_estimated_tokens,
            "covered_source_ids": list(self.covered_source_ids),
            "omitted_source_ids": list(self.omitted_source_ids),
        }


def validate_summary_candidate(
    candidate: str | bytes | Mapping[str, Any],
    *,
    context: SummaryInputContext,
    counter: SummaryTokenCounter,
) -> SummaryValidationResult:
    """Validate one model response without repairing or truncating it.

    ``final_view_text`` is the canonical summary followed by source
    attribution, exact protected facts, and authoritative action state.
    Original source content remains available through the source references
    but is not copied wholesale into the derived view.
    """

    try:
        context = validate_summary_input_context(context)
        text = _candidate_text(candidate)
        parser_issues = _strict_parse_issues(text)
        if parser_issues:
            return _invalid(context, parser_issues, counter, omitted=())
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        schema_issues = _schema_issues(value)
        if schema_issues:
            return _invalid(context, schema_issues, counter)

        known_sources = {source.source_id: source for source in context.sources}
        issues, candidate_value = _validate_value(
            value,
            context=context,
            known_sources=known_sources,
        )
        if issues:
            return _invalid(
                context,
                issues,
                counter,
                omitted=tuple(
                    source_id
                    for source_id in value.get("omitted_source_ids", ())
                    if isinstance(source_id, str)
                ),
            )

        assert candidate_value is not None
        if candidate_value.status != "OK":
            return _invalid(
                context,
                (
                    SummaryValidationIssue(
                        SummaryValidationCode.INSUFFICIENT_BUDGET,
                        "$.status",
                        candidate_value.omitted_source_ids,
                        "A partial response cannot replace original sources.",
                    ),
                ),
                counter,
                omitted=candidate_value.omitted_source_ids,
            )
        canonical_content = canonical_json(candidate_value.as_dict())
        summary_tokens = _safe_count(counter, canonical_content)
        if summary_tokens is None:
            return _invalid(
                context,
                (
                    SummaryValidationIssue(
                        SummaryValidationCode.INPUT_INVALID,
                        "$.counter",
                        detail="token counter did not return a valid count",
                    ),
                ),
                counter,
            )
        if summary_tokens > context.target_budget_tokens:
            return _invalid(
                context,
                (
                    SummaryValidationIssue(
                        SummaryValidationCode.SUMMARY_BUDGET_EXCEEDED,
                        "$",
                        detail=(f"{summary_tokens} exceeds target {context.target_budget_tokens}"),
                    ),
                ),
                counter,
                tokens=summary_tokens,
                omitted=candidate_value.omitted_source_ids,
            )

        final_view_text = _final_view_text(
            canonical_content,
            context,
            covered_source_ids=candidate_value.covered_source_ids,
        )
        final_view_tokens = _safe_count(counter, final_view_text)
        if final_view_tokens is None:
            return _invalid(
                context,
                (
                    SummaryValidationIssue(
                        SummaryValidationCode.INPUT_INVALID,
                        "$.counter",
                        detail="token counter did not return a valid count",
                    ),
                ),
                counter,
                tokens=summary_tokens,
            )
        if final_view_tokens > context.final_view_budget_tokens:
            return _invalid(
                context,
                (
                    SummaryValidationIssue(
                        SummaryValidationCode.FINAL_VIEW_BUDGET_EXCEEDED,
                        "$.final_view",
                        source_ids=candidate_value.omitted_source_ids,
                        detail=(
                            f"{final_view_tokens} exceeds final view "
                            f"{context.final_view_budget_tokens}"
                        ),
                    ),
                ),
                counter,
                tokens=summary_tokens,
                final_tokens=final_view_tokens,
                omitted=candidate_value.omitted_source_ids,
            )

        candidate_value = _replace_budget_fields(
            candidate_value,
            summary_tokens=summary_tokens,
            final_view_text=final_view_text,
            final_view_tokens=final_view_tokens,
        )
        return SummaryValidationResult(
            valid=True,
            validator_version=SUMMARY_VALIDATOR_VERSION,
            issues=(),
            candidate=candidate_value,
            summary_estimated_tokens=summary_tokens,
            final_view_estimated_tokens=final_view_tokens,
            covered_source_ids=candidate_value.covered_source_ids,
            omitted_source_ids=candidate_value.omitted_source_ids,
        )
    except (SummaryInputContextError, TypeError, ValueError, RecursionError):
        return SummaryValidationResult(
            valid=False,
            validator_version=SUMMARY_VALIDATOR_VERSION,
            issues=(
                SummaryValidationIssue(
                    SummaryValidationCode.INPUT_INVALID,
                    "$.context",
                    detail="trusted summary context is invalid",
                ),
            ),
            candidate=None,
            summary_estimated_tokens=0,
            final_view_estimated_tokens=0,
            covered_source_ids=(),
            omitted_source_ids=(),
        )


def _candidate_text(candidate: str | bytes | Mapping[str, Any]) -> str:
    if isinstance(candidate, Mapping):
        text = canonical_json(candidate)
        if len(text.encode("utf-8")) > MAX_CANDIDATE_BYTES:
            raise ValueError("candidate exceeds encoded byte limit")
        return text
    if isinstance(candidate, bytes):
        if len(candidate) > MAX_CANDIDATE_BYTES:
            raise ValueError("candidate exceeds encoded byte limit")
        return candidate.decode("utf-8", errors="strict")
    if isinstance(candidate, str):
        if len(candidate.encode("utf-8")) > MAX_CANDIDATE_BYTES:
            raise ValueError("candidate exceeds encoded byte limit")
        return candidate
    raise TypeError("candidate must be JSON text, bytes, or a mapping")


def _strict_parse_issues(text: str) -> tuple[SummaryValidationIssue, ...]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        return _issue(SummaryValidationCode.CANDIDATE_MALFORMED, "$")
    if not isinstance(value, Mapping):
        return _issue(
            SummaryValidationCode.CANDIDATE_MALFORMED,
            "$",
            detail="top-level JSON object is required",
        )
    try:
        _bounded_tree(value)
    except (TypeError, ValueError, RecursionError):
        return _issue(SummaryValidationCode.CANDIDATE_MALFORMED, "$")
    return ()


def _schema_issues(value: Any) -> tuple[SummaryValidationIssue, ...]:
    try:
        validator = Draft202012Validator(summary_output_schema())
        errors = sorted(
            validator.iter_errors(value),
            key=lambda error: (
                tuple(str(part) for part in error.absolute_path),
                error.message,
            ),
        )
    except (SchemaError, TypeError, ValueError):
        return _issue(SummaryValidationCode.SCHEMA_INVALID, "$")
    return tuple(
        SummaryValidationIssue(
            SummaryValidationCode.SCHEMA_INVALID,
            _json_path(error.absolute_path),
            detail=error.validator or "schema violation",
        )
        for error in errors
    )


def _validate_value(
    value: Mapping[str, Any],
    *,
    context: SummaryInputContext,
    known_sources: Mapping[str, Any],
) -> tuple[tuple[SummaryValidationIssue, ...], ValidatedSummary | None]:
    issues: list[SummaryValidationIssue] = []
    cited_sources: set[str] = set()
    omitted_source_ids = tuple(value["omitted_source_ids"])
    omitted_set = set(omitted_source_ids)
    _add_unknown_sources(issues, omitted_set, known_sources, "$.omitted_source_ids")

    goal_item = value.get("goal")
    if not isinstance(goal_item, Mapping):
        issues.append(
            SummaryValidationIssue(
                SummaryValidationCode.SCHEMA_INVALID,
                "$.goal",
            )
        )
        return tuple(issues), None
    goal, citations = _read_text_item(goal_item, "text", "$.goal", known_sources, issues)
    cited_sources.update(citations)
    collections: dict[str, tuple[SummaryTextItem, ...]] = {}
    for field in _TEXT_COLLECTIONS:
        items: list[SummaryTextItem] = []
        for index, raw in enumerate(value[field]):
            path = f"$.{field}[{index}]"
            item, citations = _read_text_item(raw, "text", path, known_sources, issues)
            items.append(item)
            cited_sources.update(citations)
        collections[field] = tuple(items)

    actions: list[SummaryActionItem] = []
    trusted_actions = {action.action_ref: action for action in context.completed_actions}
    seen_actions: set[str] = set()
    for index, raw in enumerate(value["completed_actions_refs"]):
        path = f"$.completed_actions_refs[{index}]"
        source_ids = tuple(raw["source_ids"])
        _add_unknown_sources(issues, set(source_ids), known_sources, f"{path}.source_ids")
        cited_sources.update(source_ids)
        action_ref = raw["action_ref"]
        if action_ref in seen_actions:
            issues.append(
                SummaryValidationIssue(
                    SummaryValidationCode.ACTION_REFERENCE_INVALID,
                    f"{path}.action_ref",
                    source_ids,
                    "duplicate action_ref",
                )
            )
        seen_actions.add(action_ref)
        authoritative = trusted_actions.get(action_ref)
        if authoritative is None:
            issues.append(
                SummaryValidationIssue(
                    SummaryValidationCode.ACTION_REFERENCE_INVALID,
                    f"{path}.action_ref",
                    source_ids,
                    "action_ref is not in the server-owned action manifest",
                )
            )
        elif set(source_ids) != set(authoritative.source_ids):
            issues.append(
                SummaryValidationIssue(
                    SummaryValidationCode.ACTION_REFERENCE_INVALID,
                    f"{path}.source_ids",
                    source_ids,
                    "action source_ids differ from the authoritative manifest",
                )
            )
        elif raw["status"] != authoritative.status.value:
            issues.append(
                SummaryValidationIssue(
                    SummaryValidationCode.ACTION_STATUS_INVALID,
                    f"{path}.status",
                    source_ids,
                    "action status differs from the authoritative manifest",
                )
            )
        actions.append(
            SummaryActionItem(
                action_ref=action_ref,
                status=SummaryActionStatus(raw["status"]),
                source_ids=source_ids,
            )
        )
    cited_action_refs = {action.action_ref for action in actions}
    for authoritative in context.completed_actions:
        if authoritative.action_ref not in cited_action_refs:
            issues.append(
                SummaryValidationIssue(
                    SummaryValidationCode.ACTION_REFERENCE_INVALID,
                    "$.completed_actions_refs",
                    authoritative.source_ids,
                    f"authoritative action {authoritative.action_ref!r} is absent",
                )
            )

    status = value["status"]
    if status == "OK":
        required_citations = {
            source_id for fact in context.protected_facts for source_id in fact.source_ids
        }
        required_citations.update(
            source_id
            for reference in context.required_references
            for source_id in reference.source_ids
        )
        required_citations.update(
            source_id for action in context.completed_actions for source_id in action.source_ids
        )
        missing = required_citations - cited_sources
        if missing:
            issues.append(
                SummaryValidationIssue(
                    SummaryValidationCode.SOURCE_RANGE_UNCOVERED,
                    "$",
                    tuple(sorted(missing)),
                    "OK summary must cite every source carrying required material",
                )
            )
    elif not omitted_set:
        issues.append(
            SummaryValidationIssue(
                SummaryValidationCode.COVERAGE_INVALID,
                "$.omitted_source_ids",
                detail="INSUFFICIENT_BUDGET must list every uncovered source",
            )
        )
    coverage_accounted = cited_sources | omitted_set
    unknown_coverage = coverage_accounted - set(context.source_ids)
    if unknown_coverage:
        issues.append(
            SummaryValidationIssue(
                SummaryValidationCode.SOURCE_OUT_OF_SCOPE,
                "$",
                tuple(sorted(unknown_coverage)),
                "summary coverage escapes the complete trusted source range",
            )
        )
    incomplete_coverage = set(context.source_ids) - coverage_accounted
    if incomplete_coverage:
        issues.append(
            SummaryValidationIssue(
                SummaryValidationCode.SOURCE_RANGE_UNCOVERED,
                "$",
                tuple(
                    source_id
                    for source_id in context.source_ids
                    if source_id in incomplete_coverage
                ),
                "every prefix source must be cited or explicitly omitted",
            )
        )
    unaccounted_at_failure = set(context.source_ids) - cited_sources
    if status == "INSUFFICIENT_BUDGET" and omitted_set != unaccounted_at_failure:
        issues.append(
            SummaryValidationIssue(
                SummaryValidationCode.COVERAGE_INVALID,
                "$.omitted_source_ids",
                tuple(
                    source_id
                    for source_id in context.source_ids
                    if source_id in omitted_set ^ unaccounted_at_failure
                ),
                "INSUFFICIENT_BUDGET omission set must equal the uncited source set",
            )
        )
    if cited_sources & omitted_set:
        issues.append(
            SummaryValidationIssue(
                SummaryValidationCode.COVERAGE_INVALID,
                "$",
                tuple(sorted(cited_sources & omitted_set)),
                "covered and omitted source sets must not overlap",
            )
        )

    _validate_protected_citations(context, cited_sources, issues)
    _validate_required_references(
        context,
        goal=goal,
        collections=collections,
        actions=tuple(actions),
        issues=issues,
    )
    if issues:
        return tuple(issues), None

    covered_source_ids = tuple(
        source_id for source_id in context.source_ids if source_id not in omitted_set
    )
    return (), ValidatedSummary(
        status=status,
        goal=goal,
        decisions=collections["decisions"],
        supported_facts=collections["supported_facts"],
        conflicts=collections["conflicts"],
        open_questions=collections["open_questions"],
        completed_actions_refs=tuple(actions),
        next_steps=collections["next_steps"],
        omitted_source_ids=omitted_source_ids,
        canonical_content="",
        summary_estimated_tokens=0,
        final_view_text="",
        final_view_estimated_tokens=0,
        covered_source_ids=covered_source_ids,
    )


def _read_text_item(
    raw: Mapping[str, Any],
    text_field: str,
    path: str,
    known_sources: Mapping[str, Any],
    issues: list[SummaryValidationIssue],
) -> tuple[SummaryTextItem, set[str]]:
    text = raw[text_field]
    if len(text) > MAX_ITEM_CHARS or "\x00" in text:
        issues.append(
            SummaryValidationIssue(
                SummaryValidationCode.CANDIDATE_MALFORMED,
                f"{path}.{text_field}",
            )
        )
    source_ids = tuple(raw["source_ids"])
    unresolved = set(source_ids) - set(known_sources)
    _add_unknown_sources(issues, unresolved, known_sources, f"{path}.source_ids")
    return SummaryTextItem(text=text, source_ids=source_ids), set(source_ids)


def _add_unknown_sources(
    issues: list[SummaryValidationIssue],
    source_ids: set[str],
    known_sources: Mapping[str, Any],
    path: str,
) -> None:
    for source_id in sorted(source_ids - set(known_sources)):
        issues.append(
            SummaryValidationIssue(
                SummaryValidationCode.SOURCE_UNKNOWN,
                path,
                (source_id,),
                "source_id is absent from the complete trusted prefix",
            )
        )


def _validate_protected_citations(
    context: SummaryInputContext,
    cited_sources: set[str],
    issues: list[SummaryValidationIssue],
) -> None:
    for fact in context.protected_facts:
        if not set(fact.source_ids).issubset(cited_sources):
            issues.append(
                SummaryValidationIssue(
                    SummaryValidationCode.PROTECTED_FACT_MISSING,
                    "$.source_ids",
                    fact.source_ids,
                    f"protected fact {fact.fact_ref!r} lacks a legal source citation",
                )
            )


def _validate_required_references(
    context: SummaryInputContext,
    *,
    goal: SummaryTextItem,
    collections: Mapping[str, Sequence[SummaryTextItem]],
    actions: Sequence[SummaryActionItem],
    issues: list[SummaryValidationIssue],
) -> None:
    text_items = [goal]
    for field in _TEXT_COLLECTIONS:
        text_items.extend(collections[field])
    for reference in context.required_references:
        required_source_ids = set(reference.source_ids)
        in_text = any(
            reference.value in item.text and required_source_ids.issubset(set(item.source_ids))
            for item in text_items
        )
        in_action = any(
            reference.value in action.action_ref
            and required_source_ids.issubset(set(action.source_ids))
            for action in actions
        )
        if not in_text and not in_action:
            issues.append(
                SummaryValidationIssue(
                    SummaryValidationCode.REFERENCE_MISSING,
                    "$",
                    reference.source_ids,
                    (
                        f"required {reference.kind.value.lower()} "
                        f"{reference.reference_id!r} is absent or bound to another source"
                    ),
                )
            )


def _final_view_text(
    canonical_content: str,
    context: SummaryInputContext,
    *,
    covered_source_ids: Sequence[str],
) -> str:
    covered = set(covered_source_ids)
    payload = {
        "untrusted_summary": {
            "trust": "UNTRUSTED_DERIVED_SUMMARY",
            "content": json.loads(canonical_content),
        },
        "source_attribution": [
            source.index_payload() for source in context.sources if source.source_id in covered
        ],
        "untrusted_omitted_sources": [
            {
                "trust": "UNTRUSTED_SOURCE",
                "source": source.index_payload(),
                "content": source.content,
            }
            for source in context.sources
            if source.source_id not in covered
        ],
        "server_protected_facts": [
            {
                "fact_ref": fact.fact_ref,
                "text": fact.text,
                "source_ids": list(fact.source_ids),
                "trust": "UNTRUSTED_SOURCE_DERIVED_DATA",
            }
            for fact in context.protected_facts
        ],
        "server_authoritative_actions": [
            {
                "action_ref": action.action_ref,
                "status": action.status.value,
                "source_ids": list(action.source_ids),
                "trust": "SERVER_AUTHORITATIVE",
            }
            for action in context.completed_actions
        ],
    }
    return canonical_json(payload)


def _replace_budget_fields(
    value: ValidatedSummary,
    *,
    summary_tokens: int,
    final_view_text: str,
    final_view_tokens: int,
) -> ValidatedSummary:
    return ValidatedSummary(
        status=value.status,
        goal=value.goal,
        decisions=value.decisions,
        supported_facts=value.supported_facts,
        conflicts=value.conflicts,
        open_questions=value.open_questions,
        completed_actions_refs=value.completed_actions_refs,
        next_steps=value.next_steps,
        omitted_source_ids=value.omitted_source_ids,
        canonical_content=canonical_json(value.as_dict()),
        summary_estimated_tokens=summary_tokens,
        final_view_text=final_view_text,
        final_view_estimated_tokens=final_view_tokens,
        covered_source_ids=value.covered_source_ids,
    )


def _safe_count(counter: SummaryTokenCounter, text: str) -> int | None:
    if not hasattr(counter, "count"):
        return None
    try:
        result = counter.count(text)
    except Exception:
        return None
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        return None
    return result


def _invalid(
    context: SummaryInputContext,
    issues: Sequence[SummaryValidationIssue],
    counter: SummaryTokenCounter,
    *,
    tokens: int = 0,
    final_tokens: int = 0,
    omitted: Sequence[str] = (),
) -> SummaryValidationResult:
    normalized_omitted = tuple(
        source_id
        for source_id in omitted
        if isinstance(source_id, str) and source_id in set(context.source_ids)
    )
    return SummaryValidationResult(
        valid=False,
        validator_version=SUMMARY_VALIDATOR_VERSION,
        issues=tuple(issues),
        candidate=None,
        summary_estimated_tokens=tokens,
        final_view_estimated_tokens=final_tokens,
        covered_source_ids=tuple(
            source_id
            for source_id in context.source_ids
            if source_id not in set(normalized_omitted)
        ),
        omitted_source_ids=normalized_omitted,
    )


def _bounded_tree(value: Any) -> None:
    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if depth > MAX_CANDIDATE_DEPTH or nodes > MAX_CANDIDATE_NODES:
            raise ValueError("candidate JSON exceeds tree limits")
        if isinstance(current, Mapping):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif current is not None and not isinstance(current, (str, int, float, bool)):
            raise ValueError("candidate contains a non-JSON value")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate JSON object key")
    return dict(pairs)


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def _json_path(parts: Sequence[Any]) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _text_item_payload(item: SummaryTextItem) -> dict[str, Any]:
    return {"text": item.text, "source_ids": list(item.source_ids)}


def _issue(
    code: SummaryValidationCode,
    path: str,
    *,
    source_ids: tuple[str, ...] = (),
    detail: str = "",
) -> tuple[SummaryValidationIssue, ...]:
    return (SummaryValidationIssue(code, path, source_ids, detail),)


__all__ = [
    "MAX_CANDIDATE_BYTES",
    "MAX_CANDIDATE_DEPTH",
    "MAX_CANDIDATE_NODES",
    "MAX_ITEM_CHARS",
    "SUMMARY_VALIDATOR_VERSION",
    "SummaryActionItem",
    "SummaryTextItem",
    "SummaryTokenCounter",
    "SummaryValidationCode",
    "SummaryValidationIssue",
    "SummaryValidationResult",
    "ValidatedSummary",
    "validate_summary_candidate",
]
