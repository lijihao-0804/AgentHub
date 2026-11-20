"""Select directly quoted sentences from the current turn's admitted paper evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePath

from packages.agent_runtime.evidence_registry import visible_evidence_registry
from packages.tools.contracts import ToolSourceKind

_QUESTION = re.compile(
    r"^(?:in|for) the (?:workspace )?(?:paper|article|study) (?P<title>[^?\n]{1,240}),\s*"
    r"(?P<task>(?:what|which|how|compare|identify|name|list)\b[^?\n]{1,500})\?\s*"
    r"(?:please )?cite (?:the |a |supporting |relevant )*(?:passages?|sources?|evidence)"
    r"(?: from (?:this|the) (?:workspace|paper))?[.!]?$",
    re.I,
)
# A short, bounded grammar deliberately excludes output-language, formatting,
# translation, recommendation and exploratory requests.
_OTHER_TASK = re.compile(
    r"\b(?:answer|respond|reply|return|write|translate|translation|recommend|suggest|"
    r"speculat\w*|brainstorm|design|your opinion|future research|in your own words|"
    r"should|might|could|related papers|similar papers|other studies)\b|"
    r"\bin [a-z]+\s*$",
    re.I,
)
_ID = re.compile(r"\barxiv:\s*(\d{4}\.\d{4,5}(?:v\d+)?)\b", re.I)
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
_HANDLE = re.compile(r"\b(?:T[0-9a-f]+)?R\d{5}E\d+\b")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]<>#!|~$])")


def quote_sources(state, messages, question: str) -> dict[str, str]:
    definition = state.get("tool_definitions", {}).get("search_knowledge")
    if (
        definition is None
        or definition.source_kind is not ToolSourceKind.BUILTIN
        or definition.retrieval_config.get("paper_answer_policy") != "verified_quotes_v1"
        or definition.retrieval_config.get("evidence_projection") != "compact_v1"
        or len(question) > 1000
    ):
        return {}
    match = _QUESTION.fullmatch(question.strip())
    if (
        not match
        or _OTHER_TASK.search(match["task"])
        or not match["task"].translate(_QUOTES).isascii()
    ):
        return {}
    title = match["title"].strip()
    ids = _ID.findall(title)
    if len(ids) > 1:
        return {}
    wanted = ids[0] if ids else title.strip('“”"').casefold()
    # Read only post-user search messages. The input registry may also contain
    # approved historical handles and Wiki claims from distinct sources.
    current = []
    for message in reversed(messages):
        if message.role == "user":
            if message.content != question:
                return {}
            break
        if message.role == "tool" and message.name == "search_knowledge":
            current.append(message)
    else:
        return {}
    registry = visible_evidence_registry(current, state.get("rag_evidence_registry", {}))
    hits: dict[str, str] = {}
    revisions = set()
    for handle, entry in registry.items():
        record = entry["record"]
        name = record.get("document_name")
        if not isinstance(name, str) or record.get("origin") not in (None, "RAW"):
            continue
        # An ID in a cited paper's bibliography is not a document identity.
        stem = PurePath(name).stem.casefold()
        matches = re.findall(r"(?<![\w.])\d{4}\.\d{4,5}(?:v\d+)?(?![\w.])", stem)
        if (ids and matches != [wanted]) or (not ids and stem != wanted):
            continue
        revisions.add(entry["reference"]["document_revision_id"])
        hits[handle] = record["snippet"]
    return hits if len(revisions) == 1 else {}


def selection_instruction(sources: Mapping[str, str]) -> str:
    return (
        "For this cited paper answer, select one to four complete source sentences that directly "
        "answer the requested facts, in the requested order. Keep each condition and qualifier. "
        "Use a separate item for each distinct point. If needed, search for a missing point. "
        'For a final answer emit only a JSON array of {"evidence_id":"...","quote":"..."} objects. '
        "Copy each quote exactly from the indicated passage (12–600 characters, at most 1200 "
        "characters total). Use only these source IDs: " + ", ".join(sources)
    )


def _unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate selection field.")
    return result


def render_quotes(raw: str, sources: Mapping[str, str]) -> str | None:
    if not isinstance(raw, str) or len(raw) > 5000:
        return None
    try:
        items = json.loads(raw, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError):
        return None
    if not isinstance(items, list) or not 1 <= len(items) <= 4:
        return None
    lines, seen = [], set()
    total = 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"evidence_id", "quote"}:
            return None
        handle, quote = item["evidence_id"], item["quote"]
        if not isinstance(handle, str) or handle not in sources or not isinstance(quote, str):
            return None
        quote = " ".join(quote.split()).translate(_QUOTES)
        total += len(quote)
        if not 12 <= len(quote) <= 600 or total > 1200 or quote in seen or _HANDLE.search(quote):
            return None
        source = " ".join(sources[handle].split())
        normalized = source.translate(_QUOTES)
        # Require a sentence boundary on both sides. A substring such as
        # "beneficial" inside "not beneficial" cannot be returned as a sentence.
        sentence = re.search(r"(?:^|(?<=[.!?]) )(" + re.escape(quote) + r")(?= |$)", normalized)
        if not sentence or not re.search(r"""[.!?]["']?$""", quote):
            return None
        start, end = sentence.span(1)
        text = _MARKDOWN.sub(r"\\\1", source[start:end])
        lines.append(f"{text} [{handle}]")
        seen.add(quote)
    return "\n\n".join(lines)
