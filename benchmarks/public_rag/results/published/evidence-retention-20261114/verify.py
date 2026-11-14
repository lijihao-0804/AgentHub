"""Recompute the published percentages from first answers and paired retrieval traces."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[4]))

from benchmarks.public_rag.evidence_retention_review import (  # noqa: E402
    get_rows,
    reconcile,
    report,
)
from benchmarks.public_rag.evidence_retention_verify import retention  # noqa: E402
from benchmarks.public_rag.qa_first_response_review import index, load, packets  # noqa: E402
from benchmarks.public_rag.qa_first_response_verify import (  # noqa: E402
    canon,
    events_of,
    require,
    sha,
)


def main():
    protocol = load(HERE / "protocol.json")
    protocol_hash = sha((HERE / "protocol.json").read_text(encoding="utf8"))
    cases, rows = get_rows(HERE / "live", protocol["cases"], protocol_hash, 0, None)
    marks = load(HERE / "judgments.json")
    packet, _ = packets(cases, rows)
    agreed, agreement = reconcile(
        packet,
        load(HERE / "judgments-first.json"),
        load(HERE / "judgments-second.json"),
        load(HERE / "adjudication.json"),
    )
    require(marks == agreed, "Independent judgments and adjudication differ")
    result = report(cases, rows, marks)
    result["independent_review_agreement"] = agreement
    result["protocol_sha256_lf"] = protocol_hash
    require(
        canon(result) == canon(load(HERE / "review.json")),
        "First-answer scores or paired losses differ",
    )
    audit = load(HERE / "verification.json")
    original_posts = original_quotes = 0
    for scope in ("independent", "historical"):
        by_arm = {}
        for arm in ("A", "B"):
            receipt = load(HERE / "live" / f"{arm}-{scope}.json")
            dry = index(load(HERE / "retrieval" / f"{arm}-{scope}.json")["rows"])
            by_arm[arm] = {}
            for row in receipt["rows"]:
                cid = row["id"]
                (reader,) = events_of(row, "reader")
                (retrieval,) = events_of(row, "retrieval")
                (prior_reader,) = events_of(dry[cid], "reader")
                (prior_pool,) = events_of(dry[cid], "retrieval")
                require(
                    reader == prior_reader
                    and retrieval["candidate"] == prior_pool["candidate"]
                    and len(reader["entries"]) == 6,
                    f"{cid}/{arm}: dry-run and paid evidence differ",
                )
                by_arm[arm][cid] = sha(canon(retrieval["candidate"]))
                physical = events_of(row, "http_wire")
                require(
                    physical
                    and all(w["prompt_sha256"] == reader["prompt_sha256"] for w in physical),
                    f"{cid}/{arm}: wire evidence mismatch",
                )
                original_posts += len(physical)
                for cite in row.get("citations", []):
                    entry = reader["entries"][cite["number"] - 1]
                    excerpt = entry["text"][
                        cite["excerpt_start_in_chunk"] : cite["excerpt_end_in_chunk"]
                    ]
                    require(
                        entry["chunk_id"] == cite["chunk_id"]
                        and excerpt == cite["excerpt"]
                        and sha(excerpt) == cite["excerpt_sha256"],
                        f"{cid}/{arm}: returned citation differs from visible text",
                    )
                    original_quotes += 1
                case = cases[cid]
                if case["negative"]:
                    continue
                texts = [
                    shown["text"].casefold()
                    for shown, ref in zip(reader["entries"], reader["chunks"], strict=True)
                    if ref["source"] in case["gold_documents"]
                ]
                hit = any(p.casefold() in t for p in case["evidence"] for t in texts)
                require(hit == audit[scope]["rows"][arm][cid]["reader"]["gold_paragraph"], cid)
            require(
                retention(audit[scope]["rows"][arm].values()) == audit[scope]["retrieval"][arm],
                "Retrieval percentages",
            )
        require(by_arm["A"] == by_arm["B"], "Paired candidate pools differ")
    print(
        f"{len(cases) * 2} first responses, {original_posts} provider POSTs, "
        f"{original_quotes} citations"
    )


if __name__ == "__main__":
    main()
