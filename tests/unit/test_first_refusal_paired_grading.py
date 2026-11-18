from copy import deepcopy

import pytest

from benchmarks.retrieval.experiments.first_refusal_contract.report import compare
from benchmarks.retrieval.experiments.first_refusal_contract.scoring import packets


def sample():
    source = dict(
        id=1,
        document="doc",
        revision="rev",
        chunk_id="chunk",
        source="file.txt",
        source_id="s",
        text="The lamp is lit.",
        locator={"start": 0},
    )
    case = dict(
        id="x",
        question="Is the lamp lit?",
        gold={"answerable": True},
        language="en",
        message_sha256="messages",
        selected=[source],
    )
    wire = dict(
        id="a",
        content='{"insufficient_evidence":false}',
        request_sha256="req",
        messages_sha256="messages",
    )
    first = dict(
        valid=True,
        refused=False,
        answer="Yes [1].",
        reason=None,
        cited=[dict(id=1, source="s", text=source["text"], locator=source["locator"])],
    )
    cite = dict(
        id=1,
        document_id="doc",
        document_revision_id="rev",
        chunk_id="chunk",
        source="file.txt",
        locator=source["locator"],
        excerpt=source["text"],
    )
    out = dict(
        id="x",
        qid="x",
        mode="live",
        cap=2048,
        http=200,
        first=first,
        events=[wire],
        body=dict(answer=first["answer"], insufficient_evidence=False, citations=[cite]),
    )
    return [case], [deepcopy(dict(out, arm=a)) for a in ("baseline", "candidate")]


def test_same_material_is_judged_once_across_arms_and_stages():
    unique, stages = packets(*sample())
    assert len(unique) == 1 and len(stages) == 4


def test_mismatched_wire_or_citation_is_rejected():
    cases, arms = sample()
    arms[1]["events"][0]["id"] = "different"
    with pytest.raises(ValueError, match="physical first response"):
        packets(cases, arms)
    cases, arms = sample()
    arms[1]["body"]["citations"][0]["document_revision_id"] = "other"
    with pytest.raises(ValueError, match="source identity"):
        packets(cases, arms)


def test_refusal_of_a_supported_answer_is_counted_even_if_aggregate_accuracy_ties():
    correct = dict(valid=True, refused=False, support=True, value=True, strict=True, reason=None)
    refused = {**correct, "refused": True, "support": False, "value": False, "strict": False}
    base = dict(
        qid="x",
        id="x",
        invocations=1,
        cost_units=1,
        answerable=True,
        model_declared_refusal=False,
        model_cited_a_declared_refusal=False,
    )
    rows = [
        dict(base, arm="baseline", first=correct, final=correct),
        dict(base, arm="candidate", first=refused, final=refused),
        dict(base, id="y", qid="y", arm="baseline", first=refused, final=refused),
        dict(base, id="y", qid="y", arm="candidate", first=correct, final=correct),
    ]
    result = compare(rows)
    assert result["first"]["gains"] == ["y"]
    assert result["first"]["losses"] == result["final"]["supported_answer_to_refusal"] == ["x"]
