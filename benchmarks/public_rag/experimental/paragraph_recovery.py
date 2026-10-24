"""Bounded original-paragraph reading views; leaf identities never change.

The frozen source map supplies paragraph parents. This is one-level reading
recovery, not a claim of a general hierarchical index or neighboring concatenation.
"""

from __future__ import annotations

from benchmarks.public_rag.source_range_coverage import locate_unique


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
