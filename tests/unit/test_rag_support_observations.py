from types import SimpleNamespace

import pytest

from benchmarks.evaluation.rag_support import RecordingRetriever, rag_observation
from packages.knowledge.contracts import (
    RetrievalQuery,
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceResult,
    RetrievalTraceStage,
)


def actual(chunk="a" * 64, snapshot="snapshot"):
    query = RetrievalQuery("question", "kb", "snapshot")
    stage = RetrievalTraceStage(1, (RetrievalTraceResult(chunk, 1, 1),))
    result = RetrievalResult(
        (SimpleNamespace(chunk_id=chunk),),
        RetrievalTrace(snapshot, stage, stage, stage, stage, 4),
    )
    return query, result


def test_citations_are_emitted_ids_not_returned_or_expected_ids():
    emitted = "b" * 64
    observation = rag_observation(f"Fact [{emitted}] [{emitted}]", [actual()], variant_hash="v")
    assert observation["citation_ids"] == [emitted]
    assert observation["rag_evidence"]["returned_chunk_ids"] == ["a" * 64]
    assert "answer" not in observation
    assert "Fact" not in str(observation)


@pytest.mark.parametrize("output", ["no citation", "[short]", "[" + "A" * 64 + "]"])
def test_missing_or_invalid_citations_stay_empty(output):
    assert rag_observation(output, [], variant_hash="v")["citation_ids"] == []


def test_snapshot_mismatch_rejected():
    with pytest.raises(ValueError, match="RETRIEVAL_SNAPSHOT_MISMATCH"):
        rag_observation("answer", [actual(snapshot="other")], variant_hash="v")


@pytest.mark.asyncio
async def test_recording_retains_actual_call_boundaries():
    query, result = actual()

    class Inner:
        async def retrieve_with_trace(self, context, received):
            assert received is query
            return result

    recording = RecordingRetriever(Inner())
    assert await recording.retrieve_with_trace(None, query) is result
    boundary = len(recording.records)
    assert await recording.retrieve(None, query) == list(result.evidence)
    assert recording.records[boundary:] == [(query, result)]
