"""Bind retrieval stages, provider attempts and endpoint citations to their actual document."""

from contextlib import contextmanager
from hashlib import sha256
from unittest.mock import patch

import httpx

from benchmarks.retrieval.experiments.first_refusal_100.attempts import (
    SCORE_FLAGS,
    digest_json,
    public_answer,
    read_attempt,
    read_http,
)
from benchmarks.retrieval.experiments.first_refusal_100.runner import observe_evidence
from packages.knowledge import citation_qa
from packages.knowledge.evidence_records import MAX_EVIDENCE_EXCERPT_CHARS


def text_hash(value):
    return sha256(value.encode("utf8")).hexdigest()


@contextmanager
def observe(doc, source_id, question):
    """Observe the genuine repository read and retrieval result, including on QA errors."""
    retriever = citation_qa.HybridKnowledgeRetriever
    original_load = retriever._load_chunks
    original_retrieve = retriever.retrieve_with_trace
    chunks = {}
    stages = {}

    async def load(instance, session, **scope):
        if (
            str(scope["snapshot_id"]) != doc["snapshot"]
            or str(scope["knowledge_base_id"]) != doc["kb"]
        ):
            raise ValueError("Hydration used another knowledge snapshot.")
        result = await original_load(instance, session, **scope)
        for cid, (chunk, document, revision) in result.items():
            if str(document.id) != doc["document"] or str(revision.id) != doc["revision"]:
                raise ValueError("A hydrated chunk belongs to another document revision.")
            chunks[cid] = {"doc_id": source_id, "text": chunk.text, "locator": chunk.locator}
        return result

    async def retrieve(instance, context, query):
        result = await original_retrieve(instance, context, query)
        if stages or query.text != question or str(result.trace.snapshot_id) != doc["snapshot"]:
            raise ValueError("Unexpected retrieval call or snapshot.")
        for name in ("dense", "sparse", "fusion", "selection", "rerank"):
            stage = getattr(result.trace, name)
            if stage is None:
                raise ValueError(f"Missing hybrid retrieval stage {name}.")
            stages[name] = [hit.chunk_id for hit in stage.results]
        return result

    with (
        patch.object(retriever, "_load_chunks", load),
        patch.object(retriever, "retrieve_with_trace", retrieve),
        observe_evidence(doc, source_id, None) as presented,
    ):
        yield {"chunks": chunks, "stages": stages, "presented": presented}


def stage_coverage(case, returned, covers):
    """The denominator for annotated-span coverage contains answerable questions only."""
    stages, chunks, visible = returned["stages"], returned["chunks"], returned["evidence"]
    if any(len(ids) != len(set(ids)) for ids in stages.values()):
        raise ValueError("A retrieval stage contains duplicate chunk identities.")
    if set(chunks) != set(stages["fusion"]) or not set(stages["selection"]).issubset(chunks):
        raise ValueError("The candidate trace differs from the scoped chunk hydration.")
    available = set(stages["selection"])
    if not set(stages["rerank"]).issubset(available):
        raise ValueError("The ranked trace contains a chunk outside the model's rerank input.")
    for position, item in enumerate(visible, 1):
        record = chunks[item["chunk_id"]]
        if (
            item["id"] != position
            or item["chunk_id"] not in available
            or any(item[key] != record[key] for key in ("doc_id", "text", "locator"))
        ):
            raise ValueError("Model-facing evidence differs from the actual scoped chunk.")
    groups = {
        "candidate": [chunks[k] for k in stages["fusion"]],
        "reranked": [chunks[k] for k in stages["rerank"]],
        "presented": visible,
    }
    return {
        name: {
            "chunks": len(records),
            "gold_covered": bool(covers(case, records)) if case["answerable"] else None,
        }
        for name, records in groups.items()
    }


def observations(case, doc, events, returned, expected_system):
    """Read the first physical output and reproduce the public answer from the final one."""
    visible = returned["evidence"]
    if not 1 <= len(events) <= 2 or not visible:
        raise ValueError("Expected one or two physical attempts with model-facing evidence.")
    first_messages = events[0]["messages"]
    if len(first_messages) != 2:
        raise ValueError("The first attempt does not start from two messages.")
    system, user = first_messages
    if (
        system != {"role": "system", "content": expected_system}
        or user["role"] != "user"
        or not user["content"].startswith(f"Question:\n{case['question']}\n\n")
        or any(e["messages"][:2] != first_messages for e in events)
    ):
        raise ValueError("The wire question, policy or presented evidence differs from the freeze.")
    if len(events) == 2:
        correction = events[1]["messages"][2:]
        if (
            len(correction) != 2
            or correction[0] != {"role": "assistant", "content": events[0]["content"]}
            or correction[1]["role"] != "user"
            or correction[1]["content"]
            not in {citation_qa._CITATION_CORRECTION, citation_qa._PROMPT_CITATION_CORRECTION}
        ):
            raise ValueError(
                "The second event is not the endpoint's correction of the first output."
            )
    attempts = [read_attempt(event, visible) for event in events]
    body = returned["body"]
    final = read_http(httpx.Response(returned["http"], json=body), doc, case["doc_id"])
    if final["state"] != "error":
        stages = {
            name: [hit["chunk_id"] for hit in stage["results"]]
            for name, stage in body["retrieval_trace"]["stages"].items()
            if stage is not None
        }
        if any(
            stages.get("fused" if k == "fusion" else k) != ids
            for k, ids in returned["stages"].items()
        ):
            raise ValueError("The endpoint stage order differs from the recorded retrieval.")
        by_id = {e["id"]: e for e in visible}
        last = events[-1]
        resolved = citation_qa.resolve_citation_answer(
            case["question"],
            last["content"],
            visible_ids=set(by_id),
            output_limit_reached=last["finish_reason"] == "length",
            contextual=False,
            answer_contract_v1=False,
        )
        if (
            final["answer"] != resolved.answer
            or (final["state"] == "refusal") != resolved.insufficient_evidence
            or [c["id"] for c in final["citations"]] != list(resolved.citation_ids)
        ):
            raise ValueError(
                "The final HTTP answer does not correspond to the last provider output."
            )
        for citation in final["citations"]:
            shown = by_id[citation["id"]]
            if (
                any(citation[key] != shown[key] for key in ("doc_id", "chunk_id", "locator"))
                or citation["text"] != shown["text"][:MAX_EVIDENCE_EXCERPT_CHARS]
            ):
                raise ValueError(
                    "A returned citation does not match the numbered evidence on the wire."
                )
    return attempts, final


def scored_answer(case, result, scorer, event, shown_hash):
    flags = scorer(
        case, state=result["state"], answer=result["answer"], citations=result["citations"]
    )
    if any(type(flags[k]) is not bool for k in SCORE_FLAGS):
        raise ValueError("Incomplete frozen structural and semantic scores.")
    language = flags["language_match"] if result["answer"].strip() else None
    if language is not None and type(language) is not bool:
        raise ValueError("Language match must be true, false or unassessable.")
    flags = dict(flags)
    semantic = flags["answer_correct"] and flags["citation_correct"]
    flags.update(
        language_match=language,
        semantic_and_citation_correct=semantic,
        joint_correct=semantic and language is True,
        value_with_language=flags["answer_correct"] and language is True,
        citation_with_language=flags["citation_correct"] and language is True,
    )
    return {
        **public_answer(result, flags),
        "presented_evidence_sha256": shown_hash,
        "citation_sha256": digest_json(result["citations"]),
        "wire_attempt": event["attempt"],
        "wire_output_sha256": text_hash(event["content"]),
    }
