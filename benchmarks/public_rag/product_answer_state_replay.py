"""Replay terminal Citation QA fields. Never contacts a model, database or network."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from packages.knowledge.citation_qa import (
    _InvalidCitationAnswer,
    _model_gateway_error,
    _normalize_validated_answer,
    _parse_model_output,
    _validate_model_output,
)
from packages.model_gateway.errors import EmptyModelResponseError

ARCHIVE = "e0fe09bbe0c7ff5b674a204606f038524b63065f"
BASE = "benchmarks/public_rag/results/published"
RESULT = Path(BASE) / "product-evidence-20261114" / "answer-state.json"
DOVER = "c80669cb444a6ec6249b971213b0226f59940a82"


def archive(name):
    blob = subprocess.check_output(["git", "show", f"{ARCHIVE}:{BASE}/{name}"])
    return json.loads(blob)


def historical_cases():
    for batch in ["refusal-consistency-20261114", "evidence-retention-20261114"]:
        protocol = archive(f"{batch}/protocol.json")
        queries = {row["id"]: row["query"] for row in protocol["cases"]}
        for arm in ["A", "B"]:
            source = f"{batch}/live/{arm}-independent.json"
            for row in archive(source)["rows"]:
                events = row["events"]
                response = [event for event in events if event["kind"] == "response"][-1]
                reader = next(event for event in events if event["kind"] == "reader")
                terminals = [event for event in events if event["kind"] == "model_output"]
                if not terminals and row["http"] != 200:
                    # The archived HTTP parse error contains no provider text to replay.
                    continue
                if terminals:
                    raw = terminals[-1]["output"]
                    provenance = "terminal_fields" if raw else "gateway_empty"
                else:
                    raw = {
                        "answer": row["answer"],
                        "insufficient_evidence": row["insufficient"],
                        "citation_ids": [item["number"] for item in row["citations"]],
                    }
                    provenance = "reconstructed_public_fields"
                query = queries[row["id"]]
                public = (
                    {
                        "answer": row["answer"],
                        "citation_ids": [item["number"] for item in row["citations"]],
                        "insufficient_evidence": row["insufficient"],
                    }
                    if row["http"] == 200
                    else {"error": row["error"], "detail": row["message"]}
                )
                expected_change = arm == "B" and (
                    (row["id"] == DOVER and batch.startswith("refusal"))
                    or ("What social media platform" in query and batch.startswith("evidence"))
                )
                if expected_change:
                    assessment = "Requested fact explicitly missing. Citations support background."
                elif "How did they obtain human intuitions" in query:
                    assessment = (
                        "Human collection procedure missing. Cited simulation details retained. "
                        "Responsiveness unresolved."
                    )
                else:
                    assessment = "preserve terminal state; no new semantic adjudication"
                yield {
                    "id": row["id"],
                    "source": source,
                    "request_id": row["request_id"],
                    "query": query,
                    "provenance": provenance,
                    "fields": raw,
                    "archived_public": public,
                    "provider_content_sha256": terminals[-1]["content_sha256"]
                    if terminals
                    else None,
                    "reader_sha256": reader["prompt_sha256"],
                    "visible_ids": [entry["number"] for entry in reader["entries"]],
                    "finish_reason": response["finish_reason"],
                    "expected_contextual_change": expected_change,
                    "expected_default_change": False,
                    "assessment": assessment,
                }


def synthetic_cases():
    def case(name, query, fields, assessment, *, error=None, change=False, finish="stop"):
        return {
            "id": name,
            "source": "synthetic",
            "query": query,
            "fields": fields,
            "visible_ids": [1],
            "finish_reason": finish,
            "expected_error": error,
            "expected_default_change": change,
            "expected_contextual_change": change,
            "assessment": assessment,
        }

    question = "Which language was used?"

    def output(answer, flag, ids):
        return {"answer": answer, "insufficient_evidence": flag, "citation_ids": ids}

    yield case(
        "global",
        question,
        output("Insufficient evidence [1].", False, [1]),
        "whole answer explicitly declines",
        change=True,
    )
    yield case(
        "reverse",
        question,
        output("Python [1].", True, [1]),
        "source [1] says Python; retain refusal-with-citations error for correction",
        error="citations_on_refusal",
    )
    yield case(
        "reverse-uncited",
        question,
        output("Python.", True, []),
        "Source [1] says Python. No citation supplied. Leave unsupported reverse flag unchanged.",
    )
    yield case(
        "partial",
        "Which language and version were used?",
        output("Python [1]. The version was not specified.", False, [1]),
        "source [1] says Python; retain useful partial answer",
    )
    yield case(
        "invalid-citation",
        question,
        output("Python [2].", False, [2]),
        "source [1] says Python but marker is invisible",
        error="citation_out_of_range",
    )
    yield case("malformed", question, "{", "nonempty invalid JSON", error="json_parse")
    yield case(
        "truncated",
        question,
        '{"answer":',
        "truncated nonempty JSON",
        error="json_parse",
        finish="length",
    )
    yield case(
        "empty",
        question,
        None,
        "gateway reports empty visible content",
        error="gateway_empty_output",
    )
    yield case(
        "empty-length",
        question,
        None,
        "gateway reports length-limited empty content",
        error="gateway_empty_output",
        finish="length",
    )
    yield case(
        "numeric-bound",
        "On average, by how much did they reduce the error?",
        output("The precise average is unclear, but the reduction exceeded 5% [1].", False, [1]),
        "partial numerical bound is useful; preserve citations",
    )
    yield case(
        "qualitative",
        "What was the average reduction?",
        output("The error improved [1].", False, [1]),
        "no number and no explicit refusal; responsiveness requires separate review",
    )


def terminal(case, mode):
    fields = case["fields"]
    if fields is None:
        error = _model_gateway_error(
            EmptyModelResponseError(output_limit_reached=case["finish_reason"] == "length")
        )
        return {"error": "gateway_empty_output", "detail": error.message}
    content = fields if isinstance(fields, str) else json.dumps(fields, ensure_ascii=False)
    try:
        output = _parse_model_output(
            content, output_limit_reached=case["finish_reason"] == "length"
        )
        ids = _validate_model_output(
            output,
            evidence_count=len(case["visible_ids"]),
            visible_ids=frozenset(case["visible_ids"]),
        )
    except _InvalidCitationAnswer as exc:
        return {"error": exc.reason.value, "detail": exc.message}
    answer, insufficient = output.answer, output.insufficient_evidence
    if mode != "legacy":
        answer, insufficient, ids = _normalize_validated_answer(
            case["query"], output, ids, contextual=mode == "contextual"
        )
    return {
        "answer": answer,
        "insufficient_evidence": insufficient,
        "citation_ids": list(ids),
    }


def replay(cases):
    rows = []
    for case in cases:
        old = terminal(case, "legacy")
        if archive_result := case.get("archived_public"):
            if old.get("error") == "gateway_empty_output":
                if archive_result != {"error": "MODEL_BAD_RESPONSE", "detail": old["detail"]}:
                    raise ValueError(f"historical provider failure mismatch: {case['id']}")
            elif old != archive_result:
                raise ValueError(f"baseline differs from archived public fields: {case['id']}")
        default = terminal(case, "default")
        contextual = terminal(case, "contextual")
        for mode, value in [("default", default), ("contextual", contextual)]:
            if (value != old) != case[f"expected_{mode}_change"]:
                raise ValueError(f"unexpected {mode} outcome: {case['source']} {case['id']}")
            if value != old and (
                not value["insufficient_evidence"]
                or value["citation_ids"]
                or value["answer"]
                != old["answer"].split(". ")[0].replace(" [1]", "").rstrip(".") + "."
            ):
                raise ValueError(f"unexpected refusal text/contract: {case['id']}")
        if case.get("expected_error") != old.get("error") and case["source"] == "synthetic":
            raise ValueError(f"unexpected error: {case['id']}")
        rows.append(
            {
                "input": case,
                "legacy": old,
                "default": default,
                "contextual": contextual,
                "default_changed": old != default,
                "contextual_changed": old != contextual,
                "rule": (
                    "whole_answer"
                    if old != default
                    else "single_requested_fact"
                    if old != contextual
                    else None
                ),
            }
        )
    return {
        "source_commit": ARCHIVE,
        "new_provider_requests": 0,
        "scope": (
            "DEV terminal response replay. One historic unarchived malformed provider text "
            "excluded. Public-field reconstructions do not recover original bytes or ID ordering."
        ),
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild-from-git", action="store_true")
    args = parser.parse_args()
    if args.rebuild_from_git:
        result = replay([*historical_cases(), *synthetic_cases()])
        RESULT.parent.mkdir(parents=True, exist_ok=True)
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    expected = json.loads(RESULT.read_text(encoding="utf8"))
    actual = replay([row["input"] for row in expected["rows"]])
    if expected != actual:
        raise ValueError("frozen replay no longer matches product code")
    print(f"verified {len(actual['rows'])} same-output cases; 0 provider calls")


if __name__ == "__main__":
    main()
