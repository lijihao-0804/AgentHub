"""Record what the model saw and distinguish its first output from the public QA result."""

import json
from decimal import Decimal
from hashlib import sha256

from benchmarks.retrieval.table_header_answers import receipt_cost
from packages.knowledge.citation_qa import (
    _InvalidCitationAnswer,
    _parse_model_output,
    _validate_model_output,
)

SCORE_FLAGS = (
    "valid",
    "refused",
    "answer_correct",
    "citation_correct",
    "joint_correct",
    "wrong_refusal",
    "unsafe_answer",
    "needs_review",
)


def digest_json(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def read_attempt(event, evidence):
    by_id = {item["id"]: item for item in evidence}
    content = event["content"]
    record = {"state": "invalid", "answer": "", "citations": [], "declared_refusal": None}
    try:
        output = _parse_model_output(
            content, output_limit_reached=event["finish_reason"] == "length"
        )
        record.update(
            answer=output.answer,
            declared_refusal=output.insufficient_evidence,
            declared_ids=output.citation_ids,
        )
        if event["finish_reason"] == "length":
            record["invalid_reason"] = "output_limit"
            return record
        ids = _validate_model_output(
            output, evidence_count=len(by_id), visible_ids=frozenset(by_id)
        )
    except _InvalidCitationAnswer as exc:
        record["invalid_reason"] = exc.reason.value
        return record
    record.update(
        state="refusal" if output.insufficient_evidence else "answer",
        citations=[by_id[i] for i in ids],
    )
    return record


def read_http(response, doc, doc_id):
    body = response.json()
    if not response.is_success:
        return {
            "state": "error",
            "answer": "",
            "citations": [],
            "error": body.get("error", {}).get("code"),
        }
    if str(body["retrieval_trace"]["snapshot_id"]) != doc["snapshot"]:
        raise ValueError("Product returned a different snapshot.")
    citations = []
    for c in body["citations"]:
        if (
            str(c["document_id"]) != doc["document"]
            or str(c["document_revision_id"]) != doc["revision"]
        ):
            raise ValueError("Product returned a citation outside this question's frozen document.")
        citations.append(
            {
                "id": c["id"],
                "doc_id": doc_id,
                "text": c["excerpt"],
                "chunk_id": c["chunk_id"],
                "locator": c["locator"],
            }
        )
    return {
        "state": "refusal" if body["insufficient_evidence"] else "answer",
        "answer": body["answer"],
        "citations": citations,
    }


def scored(case, result, scorer):
    flags = scorer(
        case, state=result["state"], answer=result["answer"], citations=result["citations"]
    )
    if any(type(flags.get(k)) is not bool for k in SCORE_FLAGS):
        raise ValueError("Scorer did not provide all frozen boolean outcomes.")
    return {k: flags[k] for k in SCORE_FLAGS}


def public_answer(result, flags):
    return {
        "state": result["state"],
        "declared_refusal": result.get("declared_refusal"),
        "invalid_reason": result.get("invalid_reason"),
        "error": result.get("error"),
        "answer_sha256": sha256(result["answer"].encode()).hexdigest(),
        "cited_chunks": len(result["citations"]),
        "scores": flags,
    }


def reconcile(directory):
    """Reprice complete receipts and match the persisted HTTP, model and score sequences."""
    freeze = json.loads((directory / "freeze.json").read_bytes())

    def read_lines(name):
        return [json.loads(line) for line in (directory / name).read_bytes().splitlines()]

    rows = read_lines("derived.jsonl")
    http = read_lines("http-private.jsonl")
    wire = read_lines("wire-private.jsonl")
    ledger = read_lines("receipts.jsonl")
    if (
        len(rows) != 2 * freeze["registered_questions"]
        or len(http) != 2 * len(rows)
        or len(ledger) != 2 * len(wire)
    ):
        raise ValueError("Incomplete persisted HTTP, attempt or receipt stream.")
    calls = 0
    paid = Decimal(0)
    reservation = Decimal(freeze["max_cny"]) / freeze["max_wire_calls"]
    unique = set()
    corrections = 0
    for ix, row in enumerate(rows):
        key = f"{row['id']}/{row['arm']}"
        started, returned = http[2 * ix : 2 * ix + 2]
        if (
            [r["key"] for r in (started, returned)] != [key, key]
            or [started["state"], returned["state"]] != ["started", "returned"]
            or returned["http"] != row["http"]
        ):
            raise ValueError("HTTP reservation, response and score do not correspond.")
        attempts = row["attempts"]
        if attempts not in (1, 2):
            raise ValueError("Unexpected number of physical model attempts.")
        correction = attempts - 1
        corrections += correction
        part = wire[calls : calls + attempts]
        if len(part) != attempts:
            raise ValueError("Missing a confirmed physical response.")
        prompt = part[0]["messages"]
        if (
            len(prompt) != 2
            or digest_json([returned["evidence"], prompt[1]["content"]]) != row["evidence_sha256"]
        ):
            raise ValueError("First wire request and persisted presented evidence differ.")
        if correction:
            retry = part[1]["messages"]
            if (
                len(retry) != 4
                or retry[:2] != prompt
                or retry[2] != {"role": "assistant", "content": part[0]["content"]}
                or retry[3]["role"] != "user"
            ):
                raise ValueError(
                    "The second wire attempt does not carry the actual first response."
                )
        amount = Decimal(0)
        for attempt, event in enumerate(part, 1):
            reserved, confirmed = ledger[2 * calls : 2 * calls + 2]
            calls += 1
            if any(
                (r["key"], r["attempt"], r["call"]) != (key, attempt, calls)
                for r in (reserved, confirmed, event)
            ):
                raise ValueError("Wire and durable ledger attempt identities differ.")
            if (
                reserved["state"] != "reserved"
                or Decimal(reserved["cny"]) != reservation
                or confirmed["state"] != "confirmed"
                or confirmed["id"] in unique
            ):
                raise ValueError("Missing reservation, unresolved charge or duplicated receipt.")
            unique.add(confirmed["id"])
            receipt = event["provider_response"]
            if set(receipt) != {"id", "model", "created", "usage"}:
                raise ValueError("Unexpected fields in the private billing projection.")
            inp, out, cache, yuan = receipt_cost(
                receipt, freeze["model"], max_input=8192, max_output=256
            )
            if (
                receipt["id"] != confirmed["id"]
                or yuan > Decimal(reserved["cny"])
                or any(
                    (r["input"], r["output"], r["cached"], Decimal(r["estimated_cny"]))
                    != (inp, out, cache, yuan)
                    for r in (event, confirmed)
                )
            ):
                raise ValueError("Reported usage or peak reservation does not reconcile.")
            amount += yuan
        if not freeze["offline"] and Decimal(row["estimated_cny"]) != amount:
            raise ValueError("Per-HTTP price differs from its physical receipts.")
        paid += amount
    if calls != len(wire) or calls > freeze["max_wire_calls"] or paid > Decimal(freeze["max_cny"]):
        raise ValueError("Cannot reconcile all physical calls under the frozen ceiling.")
    return {
        "first_http_requests": len(rows),
        "confirmed_wire_calls": calls,
        "internal_corrections": corrections,
        "estimated_cny": "0" if freeze["offline"] else str(paid),
        "unknowns": 0,
    }
