"""A fixed parent recall arm. All returnable evidence remains original indexed chunks."""

from collections.abc import Mapping, Sequence


def select_parent_chunks(
    baseline: list[dict], parent_ids: Sequence[str], best_chunk: Mapping[str, str]
) -> list[dict]:
    seen = set()
    replaceable = []
    for i, hit in enumerate(baseline):
        doc = hit["document_id"]
        if i >= 8 and doc in seen:
            replaceable.append(i)
        seen.add(doc)
    slots = replaceable[::-1][:4]
    if not slots:
        return list(baseline)
    additions = []
    for doc in parent_ids[:20]:
        if doc in seen:
            continue
        seen.add(doc)
        additions.append(
            {
                "chunk_id": best_chunk[doc],
                "document_id": doc,
                "score": baseline[-1]["score"] / (2 + len(additions)),
            }
        )
        if len(additions) == len(slots):
            break
    drop = set(slots[: len(additions)])
    return [h for i, h in enumerate(baseline) if i not in drop] + additions
