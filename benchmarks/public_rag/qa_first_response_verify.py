"""Recompute citation provenance, retrieval/reader coverage and provider receipt metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import unicodedata
from collections import Counter
from pathlib import Path

from benchmarks.public_rag.qa_first_response_review import index, load, save


def sha(text):
    return hashlib.sha256(text.encode("utf8")).hexdigest()


def canon(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def require(value, why):
    if not value:
        raise ValueError(why)


def sources(qasper, scifact):
    from benchmarks.public_rag.qa_first_response_cases import paper_text

    with scifact.open(encoding="utf8") as stream:
        sci = {
            v["_id"]: v["title"].strip() + "\n\n" + v["text"].strip()
            for line in stream
            if (v := json.loads(line))
        }
    return {"full": sci, "qasper": {i: paper_text(p) for i, p in load(qasper).items()}}


def window(text, ordinal):
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
    left = ordinal * 1000
    require(0 <= left < len(text) and (not left or left + 200 < len(text)), "Original ordinal")
    right = min(left + 1200, len(text))
    return text[left:right].strip(), left, right


def verify_citation(cite, docs, reader):
    n = cite["number"]
    require(type(n) is int and 1 <= n <= len(reader), "Citation not visible")
    shown = reader[n - 1]
    require(
        all(cite[k] == shown[k] for k in ("chunk_id", "source", "ordinal")), "Wrong reader evidence"
    )
    original, left, right = window(docs[cite["source"]], cite["ordinal"])
    h = sha(original)
    cid = sha(
        canon(
            dict(
                document_revision_id=cite["revision"],
                ordinal=cite["ordinal"],
                normalized_content_hash=h,
            )
        )
    )
    require(
        h == cite["chunk_text_sha256"] and cid == cite["chunk_id"],
        "Original chunk hash or revision",
    )
    locator = cite["locator"]
    require(
        (locator["type"], locator["char_start"], locator["char_end"])
        == ("text_range", left, right),
        "Wrong locator",
    )
    start, end = cite["excerpt_start_in_chunk"], cite["excerpt_end_in_chunk"]
    require(0 <= start < end <= len(original), "Invalid excerpt offsets")
    quote = original[start:end]
    require(quote == cite["excerpt"] and sha(quote) == cite["excerpt_sha256"], "Wrong excerpt")


def reach(case, refs, docs):
    gold = set(case["gold_documents"])
    texts = [
        window(docs[r["source"]], r["ordinal"])[0].casefold() for r in refs if r["source"] in gold
    ]
    # Official negatives have a paper ID but no evidence target. Do not count them.
    if case.get("negative") or not gold:
        return {}
    result = {"gold_source": bool(texts)}
    for name, field in (("gold_paragraph", "evidence"), ("answer_span", "answers")):
        values = case.get(field) or []
        if values:
            result[name] = any(v.casefold() in t for v in values if v for t in texts)
    return result


def events_of(row, kind):
    return [e for e in row["events"] if e["kind"] == kind]


def summarize(rows):
    times = sorted(r["elapsed_s"] for r in rows)
    responses = [e for r in rows for e in r["responses"]]
    usage = [e["usage"] for e in responses if e.get("usage") is not None]
    result = {
        "questions": len(rows),
        "http": dict(Counter(r["http"] for r in rows)),
        "elapsed_s_median": statistics.median(times),
        "elapsed_s_p95_nearest_rank": times[math.ceil(0.95 * len(times)) - 1],
        "adapter_elapsed_s_median": statistics.median(
            sum(e["elapsed_ms"] for e in r["responses"] + r["adapter_errors"]) / 1000
            for r in rows
            if r["wire_attempts"]
        )
        if any(r["wire_attempts"] for r in rows)
        else None,
        "wire_attempts": sum(r["wire_attempts"] for r in rows),
        "adapter_responses": len(responses),
        "usage_known": len(usage),
        "finish": dict(Counter(str(e.get("finish_reason")) for e in responses)),
        "tokens": {k: sum(u[k] for u in usage) for k in ("input_tokens", "output_tokens")},
        "verified_original_citations": sum(r["verified_citations"] for r in rows),
        "errors_by_observed_stage": dict(
            Counter(r["failure_observation"] for r in rows if r["failure_observation"])
        ),
    }
    result["reader_ready_failures"] = {
        m: sum(r["http"] != 200 and r["reader"].get(m, False) for r in rows)
        for m in ("gold_source", "gold_paragraph", "answer_span")
    }
    for stage in ("candidate", "reader"):
        result[stage] = {}
        for metric in ("gold_source", "gold_paragraph", "answer_span"):
            values = [r[stage][metric] for r in rows if metric in r[stage]]
            result[stage][metric] = {"hits": sum(values), "n": len(values)}
    return result


def audit_row(c, row, receipt, docs, *, cap, thinking):
    (retr,) = events_of(row, "retrieval")
    (reader,) = events_of(row, "reader")
    wire = events_of(row, "wire")
    require(retr["snapshot"] == receipt["snapshot"], "Retrieval snapshot")
    require(
        wire and all(w["prompt_sha256"] == reader["prompt_sha256"] for w in wire),
        "Reader not at adapter",
    )
    require(
        all(
            w.get("enable_thinking") is thinking
            and w["retry_limit"] == 0
            and w["max_tokens"] == cap
            and w["temperature"] == 0
            and w["model"] == receipt["model"]
            for w in wire
        ),
        "Provider mode or retries",
    )
    require(len(retr["candidate"]) <= 20 and len(reader["chunks"]) <= 6, "Retrieval budget")
    for cit in row.get("citations", []):
        verify_citation(cit, docs, reader["chunks"])
    provider_results = events_of(row, "response")
    last = (provider_results or [{}])[-1]
    failure = None
    if row["http"] != 200:
        if (
            row.get("error") == "MODEL_BAD_RESPONSE"
            and last.get("content_present") is False
            and last.get("finish_reason") == "length"
        ):
            failure = "gateway_empty_visible_content_at_length"
        elif row.get("error") == "CITATION_QA_INVALID_MODEL_OUTPUT":
            # On the historical code we can prove only this broader stage for one call.
            failure = "qa_json_or_schema" if len(wire) == 1 else "qa_output_after_correction"
            if last.get("finish_reason") == "length":
                failure += "_at_length"
        else:
            failure = "gateway_or_transport:" + str(row.get("error", "unknown"))
    return {
        "identity": [receipt[k] for k in ("model", "profile", "snapshot", "snapshot_hash")],
        "wire_settings": [(w["model"], w["max_tokens"], w["temperature"]) for w in wire],
        "digest": {
            k: sha(canon(v))
            for k, v in (
                ("pool", retr["candidate"]),
                ("final", retr["reranked"]),
                ("reader", reader["chunks"]),
            )
        },
        "prompt_sha": reader["prompt_sha256"],
        "elapsed_s": row["elapsed_s"],
        "http": row["http"],
        "verified_citations": len(row.get("citations", [])),
        "wire_attempts": len(wire),
        "responses": provider_results,
        "failure_observation": failure,
        "adapter_errors": events_of(row, "adapter_error"),
        "candidate": reach(c, retr["candidate"], docs),
        "reader": reach(c, reader["chunks"], docs),
    }


def audit(cases, a_files, b_files, corpora, protocol_sha):
    by_budget = any(c["scope"] == "reserve14" for c in cases.values())
    output = {}
    for arm, files in (("baseline", a_files), ("candidate", b_files)):
        output[arm] = {}
        for file in files:
            receipt = load(file)
            cap = 2048 if by_budget and arm == "candidate" else 512
            require(
                receipt["arm"] == (str(cap) if by_budget else arm)
                and receipt["protocol_sha256"] == protocol_sha,
                "Protocol/arm identity",
            )
            for cid, row in index(receipt["rows"]).items():
                require(
                    cid not in output[arm] and row["state"] == "returned",
                    "Duplicate/unfinished dispatch",
                )
                c = cases[cid]
                docs = corpora["full" if c["scope"] == "full" else "qasper"]
                output[arm][cid] = audit_row(
                    c,
                    row,
                    receipt,
                    docs,
                    cap=cap,
                    thinking=False if arm == "candidate" and not by_budget else None,
                )
        require(set(output[arm]) == set(cases), "Missing cases")
    parity = Counter()
    for cid in cases:
        a, b = (output[arm][cid] for arm in ("baseline", "candidate"))
        if by_budget:
            model, profile, *snap = a["identity"]
            require(
                model == b["identity"][0]
                and profile != b["identity"][1]
                and snap == b["identity"][2:],
                "Invalid budget comparison",
            )
        else:
            require(a["identity"] == b["identity"], "Different snapshots/profiles")
        for k in ("pool", "final", "reader"):
            parity[k] += int(a["digest"][k] == b["digest"][k])
        parity["reader_prompt"] += int(a["prompt_sha"] == b["prompt_sha"])
    summary = {}
    for group in ["all", *sorted({c["scope"] for c in cases.values()})]:
        summary[group] = {
            arm: summarize(
                [r for cid, r in data.items() if group == "all" or cases[cid]["scope"] == group]
            )
            for arm, data in output.items()
        }
    return {
        "paired_questions": len(cases),
        "identical": dict(parity),
        "metrics": summary,
        "rows": output,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ("protocol", "qasper", "scifact", "out"):
        p.add_argument("--" + flag, type=Path, required=True)
    for arm in ("baseline", "candidate"):
        p.add_argument("--" + arm, nargs="+", type=Path, required=True)
    args = p.parse_args()
    proto = load(args.protocol)
    require(
        hashlib.sha256(args.qasper.read_bytes()).hexdigest() == proto["train_sha256"],
        "QASPER file identity",
    )
    require(
        hashlib.sha256(args.scifact.read_bytes()).hexdigest()
        == "dec31c8182f3d744c7d2c09423756fd1d17cbef75808db13ba01cc0aab4d1ac6",
        "Frozen public SciFact file identity",
    )
    save(
        args.out,
        audit(
            index(proto["cases"]),
            args.baseline,
            args.candidate,
            sources(args.qasper, args.scifact),
            sha(args.protocol.read_text(encoding="utf8")),
        ),
    )


if __name__ == "__main__":
    main()
