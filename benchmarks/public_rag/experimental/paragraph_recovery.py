"""Bounded original-paragraph reading views; leaf identities never change.

The frozen source map supplies paragraph parents. This is one-level reading
recovery, not a claim of a general hierarchical index or neighboring concatenation.
"""

from __future__ import annotations

import json

from benchmarks.public_rag.source_range_coverage import covered_characters, locate_unique


def leaf_view(anchor, article, paper_id, revision_id):
    start, end = anchor["locator"]["char_start"], anchor["locator"]["char_end"]
    if not 0 <= start < end <= len(article):
        raise ValueError("leaf range outside immutable article")
    if article[start:end].strip() != anchor["text"]:
        raise ValueError("leaf identity does not match its original source slice")
    # Production fixed chunk normalization strips edge whitespace.
    precise = locate_unique(article, anchor["text"], bounds=(start, end))
    if precise is None:
        raise ValueError("leaf cannot be uniquely located within its declared range")
    left, right = precise
    return {
        "source_id": anchor["chunk_id"],
        "text": anchor["text"],
        "paper_id": paper_id,
        "revision_id": revision_id,
        "source_chunk_ids": [anchor["chunk_id"]],
        "source_ranges": [{"char_start": left, "char_end": right}],
        "view_kind": "ORIGINAL_LEAF",
    }


def recover_paragraphs(anchors, article, spans, paper_id, revision_id, *, max_chars=1800):
    """Select a declared parent by maximal leaf overlap, then bounded exact text.

    No question, answer, support label or human evidence is an input. Each parent
    is read at most once. Long parents yield an explicit incomplete reading view.
    """
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("positive bounded reading length required")
    parents = [p for p in spans if not p.get("is_heading") and p["text"].strip()]
    seen, views = set(), []
    for anchor in anchors:
        leaf = leaf_view(anchor, article, paper_id, revision_id)
        begin, end = anchor["locator"]["char_start"], anchor["locator"]["char_end"]
        choices = [(max(0, min(end, p["end"]) - max(begin, p["start"])), p) for p in parents]
        choices = [(overlap, p) for overlap, p in choices if overlap > 0]
        if not choices:
            if leaf["source_id"] not in seen:
                seen.add(leaf["source_id"])
                views.append(leaf)
            continue
        _, parent = max(choices, key=lambda item: (item[0], -item[1]["start"]))
        if parent["key"] in seen:
            continue
        seen.add(parent["key"])
        left, right = parent["start"], parent["end"]
        if not 0 <= left < right <= len(article) or article[left:right] != parent["text"]:
            raise ValueError("parent source mapping differs from immutable article")
        if right - left > max_chars:
            center = (max(left, begin) + min(right, end)) // 2
            left = max(left, min(center - max_chars // 2, right - max_chars))
            right = left + max_chars
        views.append(
            {
                "source_id": f"rv1:{revision_id}:{left}-{right}",
                "text": article[left:right],
                "paper_id": paper_id,
                "revision_id": revision_id,
                "source_chunk_ids": leaf["source_chunk_ids"],
                "source_ranges": [{"char_start": left, "char_end": right}],
                "parent_key": parent["key"],
                "view_kind": "BOUNDED_PARAGRAPH",
                "complete_parent": (left, right) == (parent["start"], parent["end"]),
            }
        )
    return views


def preserve_then_expand(
    baseline, anchors, article, spans, paper_id, revision_id, *, max_chars=1800, max_bytes=8000
):
    """Retain all admitted baseline intervals; only accept feasible supersets.

    Unlike parent re-selection, a shorter parent cannot discard part of a hit.
    The common baseline budget is reserved first. No gold/question is consulted.
    """
    result = list(baseline)
    if any(v["paper_id"] != paper_id or v["revision_id"] != revision_id for v in baseline):
        raise ValueError("baseline view belongs to a different source revision")
    original = [
        tuple(r["source_ranges"][0][k] for k in ("char_start", "char_end")) for r in baseline
    ]
    by_id = {a["chunk_id"]: a for a in anchors}

    def wire_size(records):
        return len(
            json.dumps(
                [{"source_id": r["source_id"], "text": r["text"]} for r in records],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    if wire_size(result) > max_bytes:
        raise ValueError("baseline already exceeds registered budget")
    expanded, skipped = 0, 0
    for i, view in enumerate(baseline):
        a, b = original[i]
        if b - a > max_chars:
            raise ValueError("reading cap cannot preserve the original leaf")
        candidate = recover_paragraphs(
            [by_id[view["source_chunk_ids"][0]]],
            article,
            spans,
            paper_id,
            revision_id,
            max_chars=max_chars,
        )[0]
        p = candidate["source_ranges"][0]
        left, right = min(a, p["char_start"]), max(b, p["char_end"])
        if right - left > max_chars:
            center = (p["char_start"] + p["char_end"]) // 2
            left = max(left, b - max_chars, min(center - max_chars // 2, a))
            right = left + max_chars
        if (left, right) == (a, b):
            continue
        if not 0 <= left <= a < b <= right <= len(article):
            raise ValueError("expanded view cannot preserve the original source interval")
        replacement = {
            **view,
            "source_id": f"rv2:{revision_id}:{left}-{right}",
            "text": article[left:right],
            "source_ranges": [{"char_start": left, "char_end": right}],
            "view_kind": "ANCHOR_PRESERVING_EXPANSION",
        }
        proposal = [*result[:i], replacement, *result[i + 1 :]]
        if wire_size(proposal) <= max_bytes:
            result, expanded = proposal, expanded + 1
        else:
            skipped += 1
    actual = [tuple(r["source_ranges"][0][k] for k in ("char_start", "char_end")) for r in result]
    if original and not covered_characters(original, actual)["complete"]:
        raise ValueError("candidate discarded admitted baseline information")
    return {
        "records": result,
        "wire_bytes": wire_size(result),
        "expanded_records": expanded,
        "expansions_skipped_for_budget": skipped,
        "baseline_coordinates_preserved": True,
        "semantic_sufficiency": "UNKNOWN",
    }
