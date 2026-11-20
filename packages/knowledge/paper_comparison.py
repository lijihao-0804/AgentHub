"""Build a bounded search phrase for explicit, English paper-comparison questions."""

import re

_OPTIONS = re.IGNORECASE
_PAPER = r"(?:the\s+)?(?:workspace\s+)?(?:paper|article|study)\s+"
_LEADING = re.compile(rf"^(?:in|for)\s+{_PAPER}(?P<title>.+?),\s*(?P<body>.+)$", _OPTIONS)
_TRAILING = re.compile(rf"^(?P<body>.+)\s+(?:in|from|of)\s+{_PAPER}(?P<title>.+)$", _OPTIONS)
_BARE_TRAILING = re.compile(r"^(?P<body>.+\bcompared)\s+in\s+(?P<title>.+)$", _OPTIONS)
_COMPARISON = re.compile(
    r"\b(?:compar(?:e|ed|ing)|comparison\s+of)\s+(?:the\s+)?(?P<relation>.+)$", _OPTIONS
)
_PASSIVE = re.compile(
    r"^how\s+(?:was|were|is|are)\s+(?:the\s+)?(?P<relation>.+?)\s+compared"
    r"(?:\s+(?P<scope>(?:with|across|between|against)\s+.+))?$",
    _OPTIONS,
)
_POSSESSIVE = re.compile(r"(?P<owners>[\w -]+s)['’]\s+(?P<subject>[\w -]+)", _OPTIONS)
_COORDINATION = re.compile(r"\b(?:and|with|between|across|against)\b", _OPTIONS)
# Generic study framing can follow a complete relation. Retain named cohorts,
# time periods, conditions, and other substantive restrictions in the search.
_STUDY_FRAME = re.compile(r"\s+(in\s+the\s+(?:[\w-]+\s+)?(?:experiment|study|trial))$", _OPTIONS)
_PREAMBLE = re.compile(
    r"\b(?:how\s+(?:did|do|does)\s+(?:the\s+)?(?:authors?|researchers?|they)|"
    r"(?:describe|explain|summarize)(?:\s+the)?|please)$",
    _OPTIONS,
)
_CITATION_REQUEST = re.compile(
    r"(?:please\s+)?cite\s+(?:(?:the|a|supporting|relevant)\s+)*"
    r"(?:passages?|sources?|evidence)(?:\s+from\s+(?:the|this)\s+(?:workspace|paper))?[.!]?",
    _OPTIONS,
)
_ARXIV = re.compile(r"(?:arxiv:\s*)?\d{4}\.\d{4,5}(?:v\d+)?", _OPTIONS)


# Be conservative about replacing a provider's own relational wording. It may
# use translations, synonyms, explicit group names, or a different word order.
_MODEL_RELATION = re.compile(
    r"\b(?:across|against|among|and|between|by|each|for each|from|of|per|versus|vs|with|"
    r"differ(?:ed|s|ent|ences?)?|vary|varied|relative|pairwise|inter[- ]\w+)\b"
    r"|\w['’]s?\b|\w['’](?=\s)",
    _OPTIONS,
)


def paper_comparison_query(question: str, *, proposed_query: str | None = None) -> str | None:
    """Apply v1 when called without a proposal, or the selective v2 rule with one."""
    # Ignore follow-on citation instructions. Long or multiline turns may contain
    # multiple requests whose scoping cannot be safely represented as one search.
    if len(question) > 1200:
        return None
    sentence, _, tail = question.strip().partition("?")
    if tail.strip() and not _CITATION_REQUEST.fullmatch(tail.strip()):
        return None
    sentence = sentence.strip().rstrip(".")
    if not sentence or len(sentence) > 800 or "\n" in sentence:
        return None
    scoped = _LEADING.fullmatch(sentence) or _TRAILING.fullmatch(sentence)
    if scoped is None:
        scoped = _BARE_TRAILING.fullmatch(sentence)
        if scoped is None:
            return None
        # For a bare "in ..." suffix require a quoted title, uppercase acronym,
        # or an explicit arXiv reference. An ordinary setting is ambiguous.
        title = scoped["title"].strip()
        if not (
            title.startswith(('"', "“", "‘"))
            or re.fullmatch(r"[A-Z][A-Z0-9-]+", title)
            or _ARXIV.search(title)
        ):
            return None
    title = scoped["title"].strip()
    if not title or len(title) > 240 or _ARXIV.fullmatch(title.strip("() ")):
        return None

    body = scoped["body"].strip().rstrip(",")
    context = ""
    passive = _PASSIVE.fullmatch(body)
    if passive:
        relation = passive["relation"]
        if passive["scope"]:
            relation += " " + passive["scope"]
    else:
        active = _COMPARISON.search(body)
        if active is None:
            return None
        prefix = body[: active.start()].strip()
        context = _PREAMBLE.sub("", prefix).strip().rstrip(",")
        relation = active["relation"]

    # Move a trailing study description past the normalized possessive. Only
    # discard an unqualified frame like "in the trial", never its modifier.
    frame = _STUDY_FRAME.search(relation)
    study = ""
    if frame:
        study = frame[1] if len(frame[1].split()) > 3 else ""
        relation = relation[: frame.start()].strip()
    # v2 can justify a rewrite only for a possessive relationship it actually
    # expands. Keep explicit comparisons and already-relational proposals intact.
    if proposed_query is not None and (
        _COORDINATION.search(relation)
        or not _POSSESSIVE.fullmatch(relation)
        or _MODEL_RELATION.search(proposed_query)
    ):
        return None
    if not _COORDINATION.search(relation):
        possessive = _POSSESSIVE.fullmatch(relation)
        if possessive:
            relation = f"{possessive['subject']} between {possessive['owners']}"
        elif not passive:
            return None
    if not relation or len(relation.split()) > 24:
        return None
    # Keep the supplied full title and identifier together for disambiguation.
    return " ".join(
        part for part in (title, context, "methods and results comparing", relation, study) if part
    )
