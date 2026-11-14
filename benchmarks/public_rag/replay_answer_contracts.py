"""Replay PR19 first answers locally. No retrieval, providers, or evaluation models."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from packages.knowledge.citation_qa import _InvalidCitationAnswer, resolve_citation_answer
from packages.knowledge.refusal_consistency import explicit_abstention

PUBLISHED = Path(__file__).parent / "results" / "published"
PRIOR = PUBLISHED / "rag-product-evidence-20261114" / "paid"
OUT = PUBLISHED / "rag-answer-contracts-v1"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(name):
    data = (PRIOR / name).read_bytes().replace(b"\r\n", b"\n")
    manifest = json.loads((PRIOR / "manifest.json").read_bytes())
    if sha(data) != manifest["artifact_sha256"][name]:
        raise ValueError(f"Published input changed: {name}")
    return json.loads(data)


def replay(row, raw, enabled, shape):
    try:
        receipt = resolve_citation_answer(
            row["question"],
            raw,
            visible_ids={v["number"] for v in row["visible"]},
            output_limit_reached=False,
            answer_contract_v1=enabled,
        )
    except _InvalidCitationAnswer as exc:
        return dict(
            parse="ok",
            terminal=exc.reason.value,
            contract_valid=False,
            state_body_consistent=None,
            source_task_correct=0 if row["labels"]["source"] != "ambiguous" else None,
            rule=exc.reason.value,
            output=None,
        )
    final = dict(
        answer=receipt.answer,
        citation_ids=list(receipt.citation_ids),
        insufficient_evidence=receipt.insufficient_evidence,
    )
    source = row["labels"]["source"]
    score = row["source_score"]
    if receipt.rule and receipt.insufficient_evidence:
        score = int(source == "unanswerable") if source != "ambiguous" else None
    return dict(
        parse="ok",
        terminal="ok",
        contract_valid=True,
        state_body_consistent=(shape == "whole_refusal") == receipt.insufficient_evidence,
        source_task_correct=score,
        rule=receipt.rule,
        output=final,
    )


def compare(row, audit, shape, note):
    event = [e for e in row["model_events"] if e["stage"] == "answer"]
    base = dict(
        id=row["id"],
        arm=row["arm"],
        source_label=row["labels"]["source"],
        pr19_group=audit["group"],
        pr19_diagnosis=audit.get("semantics"),
        question=row["question"],
        body_review=shape,
        review_basis=note,
    )
    if not event:
        if audit["group"] != "selector_failure" or row["status"] != "selector_invalid_output":
            raise ValueError("Unexpected missing answer")
        terminal = dict(
            parse=None,
            terminal=row["status"],
            contract_valid=False,
            output=None,
            state_body_consistent=None,
            rule=None,
            source_task_correct=0,
        )
        return dict(
            base,
            raw_content=None,
            raw_sha256=None,
            old=terminal,
            new=terminal,
            changed_fields=[],
            meaning_preserved=None,
            contract_only_recovery=False,
            remaining_task_error=True,
            new_wrong_refusal=False,
            lost_necessary_information=None,
            visible_refusal_reasonable=None,
            raw_state_body_consistent=None,
        )
    if len(event) != 1 or event[0]["finish_reason"] != "stop":
        raise ValueError("Unexpected generation count or finish reason")
    raw = event[0]["raw_visible_answer"]
    parsed = json.loads(raw)
    old = replay(row, raw, False, shape)
    new = replay(row, raw, True, shape)
    if old["terminal"] != row["status"] or old["source_task_correct"] != row["source_score"]:
        raise ValueError("Historical terminal/score changed")
    if old["output"] and (
        old["output"]["answer"] != row["answer"]
        or old["output"]["citation_ids"] != sorted(row["citations"])
        or old["output"]["insufficient_evidence"] != row["insufficient"]
    ):
        raise ValueError("Historical returned fields changed")
    visible = audit.get("final_visible_sufficient")
    if visible is None and audit["group"] == "rejected_cited_refusal":
        visible = audit["semantics"] == "visible_answer_missed"
    changed = [k for k in parsed if new["output"] and parsed[k] != new["output"][k]]
    return dict(
        base,
        raw_content=raw,
        raw_sha256=sha(raw.encode()),
        provider_id=event[0]["provider_id"],
        visible_ids=[v["number"] for v in row["visible"]],
        visible_sufficient_pr19=visible,
        visible_refusal_reasonable=(not visible)
        if shape == "whole_refusal" and visible is not None
        else None,
        raw_state_body_consistent=(shape == "whole_refusal") == parsed["insufficient_evidence"],
        legacy_mode_recognition=[
            explicit_abstention(row["question"], parsed["answer"], contextual=mode) is not None
            for mode in (False, True)
        ],
        old=old,
        new=new,
        changed_fields=changed,
        field_delta={k: [parsed[k], new["output"][k]] for k in changed},
        meaning_preserved=True if new["output"] else None,
        contract_only_recovery=not old["contract_valid"]
        and new["contract_valid"]
        and old["source_task_correct"] == new["source_task_correct"],
        remaining_task_error=(new["source_task_correct"] == 0)
        if new["source_task_correct"] is not None
        else None,
        new_wrong_refusal=bool(
            old["source_task_correct"] == 1
            and new["output"]
            and new["output"]["insufficient_evidence"]
            and not new["source_task_correct"]
        ),
        lost_necessary_information=False if new["output"] else None,
    )


def metrics(rows, key):
    generated = [r for r in rows if r["raw_sha256"] is not None]
    valid = [r for r in rows if r[key]["contract_valid"]]
    source = [r for r in rows if r[key]["source_task_correct"] is not None]
    refusals = [r for r in valid if r[key]["output"]["insufficient_evidence"]]
    return dict(
        opportunities=len(rows),
        generated=len(generated),
        contract_valid=[len(valid), len(rows)],
        contract_valid_given_generation=[len(valid), len(generated)],
        returned_state_body_consistent=[
            sum(r[key]["state_body_consistent"] for r in valid),
            len(valid),
        ],
        consistent_valid_per_opportunity=[
            sum(r[key]["state_body_consistent"] for r in valid),
            len(rows),
        ],
        returned_refusals=len(refusals),
        reasonable_returned_refusals=sum(r["visible_refusal_reasonable"] is True for r in refusals),
        wrong_returned_refusals=sum(r["visible_refusal_reasonable"] is False for r in refusals),
        source_task_correct=[sum(r[key]["source_task_correct"] for r in source), len(source)],
    )


def main():
    first = read("public-first-answers.json")
    audits = {(r["id"], r["arm"]): r for r in read("public-refusal-audit.json")["rows"]}
    reviews = json.loads((OUT / "replay-body-review.json").read_bytes())
    rows = []
    for row in first:
        review = reviews[row["id"]][row["arm"]]
        rows.append(compare(row, audits[row["id"], row["arm"]], review["shape"], review["basis"]))
    if (
        len(rows) != len(audits)
        or len(rows) != 120
        or sum(r["raw_sha256"] is not None for r in rows) != 119
    ):
        raise ValueError("Opportunity manifest mismatch")
    historical = [r for r in rows if r["pr19_group"] not in {"selector_failure", "other_accepted"}]
    summary = dict(
        old=metrics(rows, "old"),
        candidate=metrics(rows, "new"),
        historical_semantic_refusals=len(historical),
        legacy_recognition_on_historical_refusals=[
            sum(r["legacy_mode_recognition"][m] for r in historical) for m in (0, 1)
        ],
        raw_body_whole_refusals=sum(r["body_review"] == "whole_refusal" for r in rows),
        raw_reasonable_whole_refusals=sum(r["visible_refusal_reasonable"] is True for r in rows),
        raw_unjustified_whole_refusals=sum(r["visible_refusal_reasonable"] is False for r in rows),
        raw_state_body_consistent=[sum(r["raw_state_body_consistent"] is True for r in rows), 119],
        recovered=[
            r["id"] + "/" + r["arm"]
            for r in rows
            if not r["old"]["contract_valid"] and r["new"]["contract_valid"]
        ],
        new_failures=[
            r["id"] + "/" + r["arm"]
            for r in rows
            if r["old"]["contract_valid"] and not r["new"]["contract_valid"]
        ],
        previously_correct_converted_to_wrong_refusal=sum(r["new_wrong_refusal"] for r in rows),
        task_source_exclusions=sum(r["source_label"] == "ambiguous" for r in rows),
        paid_requests=0,
    )
    for name, data in [("replay-results.json", rows), ("replay-summary.json", summary)]:
        (OUT / name).write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
