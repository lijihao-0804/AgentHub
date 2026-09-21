from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.api.app import create_app
from apps.api.schemas.evaluation import EvaluationDatasetVersionFromRunRequest
from packages.core.config.settings import Settings
from packages.evaluation.service import derive_run_input


def test_run_to_evaluation_endpoint_is_registered() -> None:
    paths = create_app(Settings(testing=True)).openapi()["paths"]

    assert (
        "/api/v1/workspaces/{workspace_id}/evaluation/datasets/"
        "{dataset_id}/versions/from-run"
    ) in paths


def test_request_contract_is_server_owned_and_forbids_derived_fields() -> None:
    payload = {
        "run_id": "00000000-0000-0000-0000-000000000001",
        "base_version_id": "00000000-0000-0000-0000-000000000002",
        "case_key": "production-case-1",
        "split": "DEV",
        "category": "FAILURE",
        "expected": {"status": "SUCCEEDED", "failure_code": None},
        "tags": ["production"],
    }
    request = EvaluationDatasetVersionFromRunRequest.model_validate(payload)
    assert set(request.model_fields_set) == {
        "run_id",
        "base_version_id",
        "case_key",
        "split",
        "category",
        "expected",
        "tags",
    }

    for forbidden in (
        "input",
        "source_provenance",
        "ordinal",
        "schema_version",
        "observed_status",
        "observed_failure_code",
        "agent_version_id",
        "resolved_spec_hash",
    ):
        with pytest.raises(ValidationError):
            EvaluationDatasetVersionFromRunRequest.model_validate(
                {**payload, forbidden: "client-controlled"}
            )


def test_run_input_derivation_reuses_the_frozen_seven_category_contract() -> None:
    input_text = "production request"
    expected = {
        "RETRIEVAL": {"query": input_text},
        "KNOWLEDGE_QA": {"question": input_text},
        "TOOL": {"request": input_text},
        "NO_ANSWER": {"question": input_text},
        "APPROVAL": {"action": input_text},
        "MULTI_STEP": {"task": input_text},
        "FAILURE": {"scenario": input_text},
    }

    assert {category: derive_run_input(category, input_text) for category in expected} == expected
