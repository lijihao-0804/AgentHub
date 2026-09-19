"""Strict, deterministic validation for M7 evaluation dataset items."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.models import EvaluationDatasetCategory, EvaluationDatasetSplit

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "checkpoint",
        "credential",
        "customer_record",
        "encrypted_secret",
        "password",
        "raw_prompt",
        "secret",
        "system_prompt",
        "token",
        "tool_arguments",
        "tool_results",
    }
)

# The category schemas intentionally stay small in M7-A.  Later importers normalize legacy
# benchmark formats into these explicit shapes; accepting arbitrary dictionaries here would make
# dataset hashes and evaluator semantics ambiguous.
_CATEGORY_SHAPES: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {
    "RETRIEVAL": (frozenset({"query"}), frozenset({"relevant_chunk_ids"}), frozenset()),
    "KNOWLEDGE_QA": (frozenset({"question"}), frozenset({"answer", "citations"}), frozenset()),
    "TOOL": (
        frozenset({"request"}),
        frozenset({"tool_identity", "arguments"}),
        frozenset({"tool_sequence"}),
    ),
    "NO_ANSWER": (frozenset({"question"}), frozenset({"answer"}), frozenset()),
    "APPROVAL": (
        frozenset({"action"}),
        frozenset({"decision"}),
        frozenset({"approval_required"}),
    ),
    "MULTI_STEP": (
        frozenset({"task"}),
        frozenset({"steps"}),
        frozenset({"terminal_status"}),
    ),
    "FAILURE": (frozenset({"scenario"}), frozenset({"status", "failure_code"}), frozenset()),
}


def validate_dataset_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AgentHubError("EVALUATION_DATASET_INVALID", "Dataset item must be an object.", 422)
    expected_keys = {
        "case_key",
        "split",
        "category",
        "input",
        "expected",
        "tags",
        "source_provenance",
        "ordinal",
    }
    if set(raw) != expected_keys:
        raise AgentHubError(
            "EVALUATION_DATASET_INVALID",
            "Dataset item fields do not match the M7 schema.",
            422,
        )

    case_key = raw["case_key"]
    if not isinstance(case_key, str) or not case_key.strip() or len(case_key) > 200:
        raise AgentHubError("EVALUATION_DATASET_INVALID", "case_key is invalid.", 422)
    split = _enum_value(raw["split"], EvaluationDatasetSplit, "split")
    category = _enum_value(raw["category"], EvaluationDatasetCategory, "category")
    input_value = _object(raw["input"], "input")
    expected_value = _object(raw["expected"], "expected")
    tags = raw["tags"]
    if (
        not isinstance(tags, Sequence)
        or isinstance(tags, (str, bytes))
        or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
    ):
        raise AgentHubError("EVALUATION_DATASET_INVALID", "tags must be a list of strings.", 422)
    source = _object(raw["source_provenance"], "source_provenance")
    if not isinstance(source.get("source_kind"), str) or not isinstance(
        source.get("source_id"), str
    ):
        raise AgentHubError(
            "EVALUATION_DATASET_INVALID",
            "source_provenance requires source_kind and source_id.",
            422,
        )
    ordinal = raw["ordinal"]
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise AgentHubError("EVALUATION_DATASET_INVALID", "ordinal must be non-negative.", 422)
    _reject_secret_keys({"input": input_value, "expected": expected_value, "source": source})

    required_input, required_expected, optional_expected = _CATEGORY_SHAPES[category]
    if (
        set(input_value) != required_input
        or not required_expected.issubset(expected_value)
        or not set(expected_value).issubset(required_expected | optional_expected)
    ):
        raise AgentHubError(
            "EVALUATION_DATASET_INVALID",
            f"{category} input/expected fields do not match the category schema.",
            422,
        )
    _validate_category_types(category, input_value, expected_value)
    return {
        "case_key": case_key.strip(),
        "split": split,
        "category": category,
        "input": dict(input_value),
        "expected": dict(expected_value),
        "tags": list(tags),
        "source_provenance": dict(source),
        "ordinal": ordinal,
    }


def validate_dataset_items(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not items:
        raise AgentHubError(
            "EVALUATION_DATASET_INVALID", "A dataset version requires at least one item.", 422
        )
    normalized = [validate_dataset_item(item) for item in items]
    case_keys = [item["case_key"] for item in normalized]
    if len(case_keys) != len(set(case_keys)):
        raise AgentHubError(
            "EVALUATION_DATASET_DUPLICATE_CASE_KEY",
            "case_key must be unique within a dataset version.",
            422,
        )
    ordinals = [item["ordinal"] for item in normalized]
    if len(ordinals) != len(set(ordinals)):
        raise AgentHubError(
            "EVALUATION_DATASET_INVALID", "ordinal must be unique within a dataset version.", 422
        )

    input_hashes: set[str] = set()
    provenance_hashes: set[str] = set()
    for item in normalized:
        input_identity = canonical_json_hash({"category": item["category"], "input": item["input"]})
        if input_identity in input_hashes:
            raise AgentHubError(
                "EVALUATION_DATASET_DUPLICATE_INPUT",
                "Dataset items must not repeat a normalized input across splits.",
                422,
            )
        input_hashes.add(input_identity)
        provenance_identity = canonical_json_hash(item["source_provenance"])
        if provenance_identity in provenance_hashes:
            raise AgentHubError(
                "EVALUATION_DATASET_DUPLICATE_PROVENANCE",
                "Dataset source provenance must identify each source case once.",
                422,
            )
        provenance_hashes.add(provenance_identity)
    return sorted(normalized, key=lambda item: (item["ordinal"], item["case_key"]))


def dataset_content_hash(items: Sequence[Mapping[str, Any]], *, schema_version: int = 1) -> str:
    normalized = validate_dataset_items(items)
    return canonical_json_hash({"schema_version": schema_version, "items": normalized})


def validate_variant_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentHubError(
            "EVALUATION_VARIANT_METADATA_INVALID",
            "Variant metadata must be an object.",
            422,
        )
    _reject_secret_keys(value)
    normalized = dict(value)
    try:
        canonical_json_hash(normalized)
    except (TypeError, ValueError):
        raise AgentHubError(
            "EVALUATION_VARIANT_METADATA_INVALID",
            "Variant metadata must be JSON-compatible.",
            422,
        ) from None
    return normalized


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentHubError("EVALUATION_DATASET_INVALID", f"{field} must be an object.", 422)
    return dict(value)


def _enum_value(value: Any, enum_type: type, field: str) -> str:
    try:
        return enum_type(value).value
    except (TypeError, ValueError):
        raise AgentHubError("EVALUATION_DATASET_INVALID", f"{field} is invalid.", 422) from None


def _reject_secret_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                raise AgentHubError(
                    "EVALUATION_DATASET_SECRET_FORBIDDEN",
                    "Dataset content must not contain credentials or tokens.",
                    422,
                )
            _reject_secret_keys(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_secret_keys(item)


def _validate_category_types(
    category: str, input_value: Mapping[str, Any], expected_value: Mapping[str, Any]
) -> None:
    if category in {"RETRIEVAL", "KNOWLEDGE_QA", "NO_ANSWER"} and not isinstance(
        input_value.get("query", input_value.get("question")), str
    ):
        raise AgentHubError("EVALUATION_DATASET_INVALID", "The query/question must be text.", 422)
    if category == "RETRIEVAL" and not _string_list(expected_value["relevant_chunk_ids"]):
        raise AgentHubError("EVALUATION_DATASET_INVALID", "Retrieval ground truth is invalid.", 422)
    if category == "KNOWLEDGE_QA":
        if not isinstance(expected_value["answer"], str) or not _string_list(
            expected_value["citations"]
        ):
            raise AgentHubError(
                "EVALUATION_DATASET_INVALID",
                "Knowledge QA expected data is invalid.",
                422,
            )
    if category == "TOOL":
        if not isinstance(expected_value["tool_identity"], str) or not isinstance(
            expected_value["arguments"], Mapping
        ):
            raise AgentHubError("EVALUATION_DATASET_INVALID", "Tool expected data is invalid.", 422)
        if "tool_sequence" in expected_value and not _string_list(expected_value["tool_sequence"]):
            raise AgentHubError("EVALUATION_DATASET_INVALID", "Tool sequence is invalid.", 422)
    if category == "NO_ANSWER" and not isinstance(expected_value["answer"], str):
        raise AgentHubError(
            "EVALUATION_DATASET_INVALID", "No-answer expected data is invalid.", 422
        )
    if category == "APPROVAL" and not isinstance(expected_value["decision"], str):
        raise AgentHubError("EVALUATION_DATASET_INVALID", "Approval expected data is invalid.", 422)
    if (
        category == "APPROVAL"
        and "approval_required" in expected_value
        and not isinstance(expected_value["approval_required"], bool)
    ):
        raise AgentHubError("EVALUATION_DATASET_INVALID", "Approval requirement is invalid.", 422)
    if category == "MULTI_STEP" and not _string_list(expected_value["steps"]):
        raise AgentHubError(
            "EVALUATION_DATASET_INVALID", "Multi-step expected data is invalid.", 422
        )
    if (
        category == "MULTI_STEP"
        and "terminal_status" in expected_value
        and not isinstance(expected_value["terminal_status"], str)
    ):
        raise AgentHubError(
            "EVALUATION_DATASET_INVALID", "Multi-step terminal status is invalid.", 422
        )
    if category == "FAILURE":
        if not isinstance(expected_value["status"], str) or not (
            expected_value["failure_code"] is None
            or isinstance(expected_value["failure_code"], str)
        ):
            raise AgentHubError(
                "EVALUATION_DATASET_INVALID", "Failure expected data is invalid.", 422
            )


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


__all__ = [
    "dataset_content_hash",
    "validate_dataset_item",
    "validate_dataset_items",
    "validate_variant_metadata",
]
