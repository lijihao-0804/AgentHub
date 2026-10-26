"""Resolve citations against visible snapshot records, without scoring prose."""

from __future__ import annotations

import re


def citation_identity_audit(answer, records):
    by_chunk = {record["chunk_id"]: record for record in records}
    by_source = {}
    for record in records:
        source_id = record.get("source_id") or record.get("document_name", "").split("__")[0]
        by_source.setdefault(source_id, []).append(record["chunk_id"])
    cited_chunks = list(dict.fromkeys(re.findall(r"\b[0-9a-f]{64}\b", answer or "")))
    cited_sources = list(dict.fromkeys(re.findall(r"\bPOL-[A-Z0-9-]+", answer or "")))
    identities = []
    for value in cited_chunks:
        identities.append(
            {
                "id": value,
                "kind": "chunk_id",
                "visible": value in by_chunk,
                "visible_chunk_ids": [value] if value in by_chunk else [],
            }
        )
    for value in cited_sources:
        identities.append(
            {
                "id": value,
                "kind": "source_id",
                "visible": value in by_source,
                "visible_chunk_ids": by_source.get(value, []),
            }
        )
    return {
        "status": "MISSING"
        if not identities
        else "VALID"
        if all(identity["visible"] for identity in identities)
        else "INVALID",
        "identities": identities,
        "semantic_claim_support": "requires separate source-grounded review",
    }
