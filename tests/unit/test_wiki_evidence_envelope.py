"""W0 unified evidence envelope: origin markers, fail-closed registration."""

import json

import pytest

from packages.agent_runtime.evidence_registry import (
    RAG_EVIDENCE_TOOL_IDENTITIES,
    visible_evidence_registry,
)
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.contracts import EvidenceOrigin, RetrievalTrace
from packages.knowledge.evidence_plan import assemble_evidence_plan, validate_evidence_view
from packages.model_gateway.contracts import ModelMessage
from packages.wiki.evidence_envelope import (
    EVIDENCE_ORIGIN_RAW,
    EVIDENCE_ORIGIN_WIKI_DERIVED,
    envelope_violation,
    stamp_raw_evidence,
    stamp_wiki_derived_evidence,
    validate_wiki_derived_record,
    wiki_evidence_reference,
)

_PUBLICATION_ID = "0f0e0d0c-1111-2222-3333-444455556666"
_PUBLICATION_HASH = "a" * 64


def _claim(ordinal, *, page_key="page-1", quote="原始引文"):
    return {
        "page_key": page_key,
        "topic_key": "topic",
        "quote": quote,
        "chunk_id": f"chunk-{ordinal}",
        "document_id": f"doc-{ordinal}",
        "document_revision_id": f"rev-{ordinal}",
        "locator": {"page": ordinal},
        "predicate": "有效期",
        "value": "7年",
        "condition": None,
        "conflict": False,
        "conflict_peers": [],
        "conflict_origin": "BASIC_OR_MODEL",
    }


def _reference(record, snapshot_id="snap-1", knowledge_base_id="kb-1"):
    return wiki_evidence_reference(
        record, snapshot_id=snapshot_id, knowledge_base_id=knowledge_base_id
    )


def test_stamp_raw_evidence_adds_origin_and_keeps_fields_unchanged():
    raw = {
        "quote": "raw text",
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "document_revision_id": "rev-1",
        "locator": {"page": 1},
        "conflict": False,
        "source": "raw_retrieval",
        "retrieval_score": 0.5,
        "rerank_score": None,
    }
    stamped = stamp_raw_evidence([raw])
    assert stamped[0]["origin"] == EVIDENCE_ORIGIN_RAW
    assert stamped[0]["source"] == "raw_retrieval"
    assert stamped[0]["retrieval_score"] == 0.5
    for key, value in raw.items():
        assert stamped[0][key] == value
    assert "origin" not in raw


def test_stamp_raw_evidence_rejects_records_already_claiming_wiki_layer():
    with pytest.raises(AgentHubError) as error:
        stamp_raw_evidence([{"chunk_id": "c", "origin": EVIDENCE_ORIGIN_WIKI_DERIVED}])
    assert error.value.code == "WIKI_ENVELOPE_INVALID"


def test_stamp_wiki_derived_evidence_groups_source_revisions_by_page():
    claims = [
        _claim(1),
        _claim(2, page_key="page-1"),
        _claim(3, page_key="page-2"),
    ]
    stamped = stamp_wiki_derived_evidence(
        claims, publication_id=_PUBLICATION_ID, publication_hash=_PUBLICATION_HASH
    )
    assert all(item["origin"] == EVIDENCE_ORIGIN_WIKI_DERIVED for item in stamped)
    assert all(item["publication_id"] == _PUBLICATION_ID for item in stamped)
    assert all(item["publication_hash"] == _PUBLICATION_HASH for item in stamped)
    assert stamped[0]["wiki_page_id"] == "page-1"
    assert stamped[0]["source_revisions"] == ["rev-1", "rev-2"]
    assert stamped[2]["wiki_page_id"] == "page-2"
    assert stamped[2]["source_revisions"] == ["rev-3"]
    # Stamping never mutates its input.
    assert "origin" not in claims[0]


@pytest.mark.parametrize("field", ["publication_id", "publication_hash"])
def test_stamp_wiki_derived_evidence_fails_closed_without_publication_identity(field):
    kwargs = {"publication_id": _PUBLICATION_ID, "publication_hash": _PUBLICATION_HASH}
    kwargs[field] = ""
    with pytest.raises(AgentHubError) as error:
        stamp_wiki_derived_evidence([_claim(1)], **kwargs)
    assert error.value.code == "WIKI_ENVELOPE_INVALID"


def test_stamp_wiki_derived_evidence_fails_closed_without_page_or_revision():
    with pytest.raises(AgentHubError):
        stamp_wiki_derived_evidence(
            [{**_claim(1), "page_key": ""}],
            publication_id=_PUBLICATION_ID,
            publication_hash=_PUBLICATION_HASH,
        )
    with pytest.raises(AgentHubError):
        stamp_wiki_derived_evidence(
            [{**_claim(1), "document_revision_id": None}],
            publication_id=_PUBLICATION_ID,
            publication_hash=_PUBLICATION_HASH,
        )


def test_envelope_violation_reports_missing_publication_hash():
    record = stamp_wiki_derived_evidence(
        [_claim(1)], publication_id=_PUBLICATION_ID, publication_hash=_PUBLICATION_HASH
    )[0]
    assert envelope_violation(record) is None
    broken = {key: value for key, value in record.items() if key != "publication_hash"}
    assert "publication_hash" in envelope_violation(broken)
    assert envelope_violation({**record, "source_revisions": []}) is not None
    assert envelope_violation({**record, "quote": ""}) is not None
    assert envelope_violation({"origin": "MYSTERY_LAYER"}) is not None
    assert envelope_violation({"chunk_id": "c"}) is None


def test_validate_wiki_derived_record_raises_wiki_envelope_invalid():
    with pytest.raises(AgentHubError) as error:
        validate_wiki_derived_record({"origin": EVIDENCE_ORIGIN_WIKI_DERIVED})
    assert error.value.code == "WIKI_ENVELOPE_INVALID"


def test_registration_rejects_wiki_derived_record_missing_publication_hash():
    record = stamp_wiki_derived_evidence(
        [_claim(1)], publication_id=_PUBLICATION_ID, publication_hash=_PUBLICATION_HASH
    )[0]
    record = {
        **record,
        "snippet": record["quote"][:2000],
    }
    broken = {key: value for key, value in record.items() if key != "publication_hash"}
    complete_plan = assemble_evidence_plan(
        [record],
        [_reference(record)],
        budget=8_000,
        estimator=len,
        citation_prefix="R00001E",
        record_validator=envelope_violation,
        allow_shared_references=True,
    )
    assert complete_plan is not None
    assert (
        assemble_evidence_plan(
            [broken],
            [_reference(record)],
            budget=8_000,
            estimator=len,
            citation_prefix="R00001E",
            record_validator=envelope_violation,
            allow_shared_references=True,
        )
        is None
    )


def test_registry_keeps_publication_hash_private_to_the_checkpoint_registry():
    record = stamp_wiki_derived_evidence(
        [_claim(1)], publication_id=_PUBLICATION_ID, publication_hash=_PUBLICATION_HASH
    )[0]
    record["snippet"] = record["quote"][:2000]
    plan = assemble_evidence_plan(
        [record],
        [_reference(record)],
        budget=8_000,
        estimator=len,
        citation_prefix="R00001E",
        record_validator=envelope_violation,
        allow_shared_references=True,
    )
    assert plan is not None
    item = plan.payload["results"][0]
    assert item["origin"] == EVIDENCE_ORIGIN_WIKI_DERIVED
    assert item["wiki_page_id"] == "page-1"
    assert item["snippet"] == "原始引文"
    assert "publication_hash" not in json.dumps(plan.payload)
    assert "publication_id" not in json.dumps(plan.payload)
    entry = plan.registry["R00001E1"]
    assert entry["record"]["publication_hash"] == _PUBLICATION_HASH
    assert entry["record"]["publication_id"] == _PUBLICATION_ID
    assert entry["record"]["source_revisions"] == ["rev-1"]
    assert entry["reference"] == _reference(record)


def test_raw_record_view_is_byte_unchanged_without_origin_fields():
    raw = {
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "document_revision_id": "rev-1",
        "locator": {"page": 1},
        "snippet": "raw text",
    }
    ref = _reference(raw)
    plan = assemble_evidence_plan([raw], [ref], budget=8_000, estimator=len, citation_prefix="E")
    assert plan is not None
    assert plan.payload["results"][0] == {
        "id": "E1",
        "locator": {"page": 1},
        "snippet": "raw text",
    }
    assert plan.registry["E1"]["reference"] == ref
    assert plan.registry["E1"]["record"] == raw


def test_shared_chunk_claims_register_side_by_side_only_when_allowed():
    first = stamp_wiki_derived_evidence(
        [_claim(1)], publication_id=_PUBLICATION_ID, publication_hash=_PUBLICATION_HASH
    )[0]
    second = stamp_wiki_derived_evidence(
        [
            {
                **_claim(1),
                "quote": "同一分块的另一条引文",
            }
        ],
        publication_id=_PUBLICATION_ID,
        publication_hash=_PUBLICATION_HASH,
    )[0]
    for record in (first, second):
        record["snippet"] = record["quote"][:2000]
    records = [first, second]
    refs = [_reference(first), _reference(second)]
    assert (
        assemble_evidence_plan(
            records,
            refs,
            budget=8_000,
            estimator=len,
            citation_prefix="R00001E",
            record_validator=envelope_violation,
        )
        is None
    )
    plan = assemble_evidence_plan(
        records,
        refs,
        budget=8_000,
        estimator=len,
        citation_prefix="R00001E",
        record_validator=envelope_violation,
        allow_shared_references=True,
    )
    assert plan is not None
    assert [item["id"] for item in plan.payload["results"]] == ["R00001E1", "R00001E2"]
    assert plan.payload["results"][1]["snippet"] == "同一分块的另一条引文"


def test_evidence_view_validation_accepts_origin_and_rejects_inconsistent_markers():
    record = stamp_wiki_derived_evidence(
        [_claim(1)], publication_id=_PUBLICATION_ID, publication_hash=_PUBLICATION_HASH
    )[0]
    record["snippet"] = record["quote"][:2000]
    plan = assemble_evidence_plan(
        [record],
        [_reference(record)],
        budget=8_000,
        estimator=len,
        citation_prefix="R00001E",
        record_validator=envelope_violation,
        allow_shared_references=True,
    )
    assert plan is not None and validate_evidence_view(plan.payload)
    item = dict(plan.payload["results"][0])
    assert validate_evidence_view({**plan.payload, "results": [item]})
    # A raw item may not smuggle a wiki page id.
    raw_item = {**item, "origin": EVIDENCE_ORIGIN_RAW}
    assert not validate_evidence_view({**plan.payload, "results": [raw_item]})
    # An unknown origin value is rejected outright.
    assert not validate_evidence_view({**plan.payload, "results": [{**item, "origin": "OTHER"}]})
    # WIKI_DERIVED without a page identity is rejected.
    no_page = {key: value for key, value in item.items() if key != "wiki_page_id"}
    assert not validate_evidence_view({**plan.payload, "results": [no_page]})


def test_visible_evidence_registry_accepts_query_wiki_messages_and_fails_closed():
    record = stamp_wiki_derived_evidence(
        [_claim(1)], publication_id=_PUBLICATION_ID, publication_hash=_PUBLICATION_HASH
    )[0]
    record["snippet"] = record["quote"][:2000]
    plan = assemble_evidence_plan(
        [record],
        [_reference(record)],
        budget=8_000,
        estimator=len,
        citation_prefix="R00001E",
        record_validator=envelope_violation,
        allow_shared_references=True,
    )
    assert plan is not None
    message = ModelMessage("tool", json.dumps(plan.payload), "query_wiki", "call-1")
    registry = visible_evidence_registry([message], plan.registry)
    assert set(registry) == {"R00001E1"}
    assert registry["R00001E1"]["record"]["publication_hash"] == _PUBLICATION_HASH
    # A visible item edited after projection cannot re-enter the registry.
    tampered = json.loads(json.dumps(plan.payload))
    tampered["results"][0]["snippet"] = "篡改后的引文"
    with pytest.raises(AgentHubError) as error:
        visible_evidence_registry(
            [ModelMessage("tool", json.dumps(tampered), "query_wiki", "call-1")],
            plan.registry,
        )
    assert error.value.code == "RAG_EVIDENCE_MAPPING_INVALID"


def test_rag_evidence_tool_identities_cover_both_tools():
    assert RAG_EVIDENCE_TOOL_IDENTITIES == {"search_knowledge", "query_wiki"}


def test_retrieval_trace_wiki_stage_defaults_to_none():
    trace = RetrievalTrace(
        snapshot_id="snap",
        dense=None,
        sparse=None,
        fusion=None,
        rerank=None,
        total_latency_ms=1.0,
    )
    assert trace.wiki is None
    assert trace.evidence_set is None


def test_evidence_origin_enum_values_are_stable():
    assert EvidenceOrigin.RAW.value == "RAW"
    assert EvidenceOrigin.WIKI_DERIVED.value == "WIKI_DERIVED"
