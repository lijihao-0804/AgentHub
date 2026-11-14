"""Rebuild the content-free historical 502 ledger from original local captures, offline."""

import hashlib
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "benchmarks/public_rag").is_dir())
RAW = ROOT / ".scratch/article-live-20261114"
LIVE = HERE.parents[1] / "article-recall-20261114/live"
ARMS = ("baseline", "article_local_passage", "full_parent4")


def read(path):
    return json.loads(path.read_bytes())


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def get_spans(log):
    groups = {}
    for number, line in enumerate(log.read_bytes().splitlines(), start=1):
        if not line.startswith(b"{"):
            continue
        event = json.loads(line)
        span = event.get("trace_span")
        if not span:
            continue
        request = event["request_id"]
        if request not in groups:
            groups[request] = []
        assert next(reversed(groups)) == request, "requests interleaved in a serial run"
        groups[request].append((number, span))
    return list(groups.values())


def build():
    sources, failures = {}, []
    counts = Counter()
    protocol = read(LIVE / "protocol-public.json")
    for corpus in ("full", "qasper"):
        case_ids = [case["id"] for case in protocol["cases"] if case["scope"] == corpus]
        for arm in ARMS:
            stem = f"{corpus}-{arm}"
            public = LIVE / f"{stem}.json"
            raw = RAW / f"{stem}-raw.json"
            log = RAW / f"{stem}-api.log"
            rows = read(raw)["rows"]
            published = read(public)["rows"]
            groups = get_spans(log)
            assert [r["id"] for r in rows] == case_ids == [r["id"] for r in published]
            sources[stem] = {p.name: fingerprint(p) for p in (public, raw, log)}
            for index, (r, pub, spans) in enumerate(zip(rows, published, groups, strict=True), 1):
                assert r["state"] == "returned"
                assert all(r.get(k) == pub.get(k) for k in ("state", "http", "elapsed_s", "error"))
                counts[r["http"]] += 1
                if r["http"] != 502:
                    continue
                assert [s["span"] for _, s in spans] == [
                    "knowledge.rerank",
                    "knowledge.retrieve",
                    "model.generate",
                ]
                (rerank_line, rerank), (retrieve_line, retrieval), (model_line, model) = spans
                assert rerank["status"] == retrieval["status"] == "ok"
                assert rerank["final_count"] == retrieval["rerank_output_count"] == 6
                assert model["attempt_count"] == 1 and model["fallback_used"] is False
                if r["error"] == "MODEL_BAD_RESPONSE":
                    assert model["status"] == "error" and model["failure_code"] == r["error"]
                    stage = "model_gateway_bad_response"
                else:
                    assert r["error"] == "CITATION_QA_INVALID_MODEL_OUTPUT"
                    assert model["status"] == "ok" and model["failure_code"] is None
                    stage = "qa_unique_json_or_strict_output_schema"
                failures.append(
                    {
                        "scope": corpus,
                        "arm": arm,
                        "case_id": r["id"],
                        "row_1_based": index,
                        "http": r["http"],
                        "elapsed_s": r["elapsed_s"],
                        "code": r["error"],
                        "api_log": log.name,
                        "rerank_line": rerank_line,
                        "retrieve_line": retrieve_line,
                        "model_line": model_line,
                        "retrieval_status": retrieval["status"],
                        "rerank_output_count": rerank["final_count"],
                        "model_status": model["status"],
                        "model_attempt_count": model["attempt_count"],
                        "fallback_used": model["fallback_used"],
                        "stage": stage,
                        "stage_confidence": "high (serial order join)",
                        "exact_subreason": None,
                    }
                )
    assert counts == {200: 42, 502: 9}
    return {
        "source_run": "article-recall-20261114/live",
        "scope": "51 predeclared first HTTP outcomes; separate title diagnostic was HTTP 200.",
        "join": (
            "Each arm used a dedicated API process and serial client. "
            "Match request groups in log order to frozen case order. "
            "No request ID in the client capture. Six group counts match six full captures exactly."
        ),
        "baseline_commit": read(LIVE / "provenance.json")["baseline_commit"],
        "http_counts": dict(counts),
        "sha256": sources,
        "failures": failures,
    }


if __name__ == "__main__":
    out = HERE / "first-502.json"
    out.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
