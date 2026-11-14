"""Recognize explicit whole-answer abstentions in structurally valid Citation QA replies."""

from __future__ import annotations

import re

_CITATION = re.compile(r"\s*\[-?\d+\]")
_WHOLE_ANSWER = re.compile(
    r"(?:(?:there is )?(?:insufficient evidence|not enough (?:evidence|information))"
    r"(?: to answer (?:this|the) question)?"
    r"|the (?:provided |supplied |available )?evidence is insufficient"
    r"(?: to answer (?:this|the) question)?"
    r"|(?:this|the) question cannot be answered (?:from|using) the "
    r"(?:(?:provided|supplied|available) )?(?:evidence|information|material)"
    r"|(?:i|we) cannot answer (?:this|the) question (?:from|using) the "
    r"(?:(?:provided|supplied|available) )?(?:evidence|information|material)"
    r")\.?",
    re.IGNORECASE,
)
_WHOLE_ANSWER_ZH = re.compile(
    r"(?:(?:所提供的|提供的|现有的?)?(?:证据|资料|信息)不足(?:[，,]?\s*"
    r"(?:无法|不足以)回答(?:这个|该|此)问题)?"
    r"|(?:无法|不能)根据(?:所提供的|提供的|现有|现有的)(?:证据|资料|信息)"
    r"回答(?:这个|该|此)问题)[。.]?"
)

# Recognize a single requested entity. Restrict the shape of *both* the query and
# the reply: an unqualified "does not name" about an extra attribute, another
# population, or a second part of a question cannot justify dropping citations.
_SOURCE_PREFIX = re.compile(
    r'^in (?:the )?(?:paper|article|document) (?:“[^”]+”|"[^"]+")\s*,\s*', re.I
)
_ENTITY_QUESTION = re.compile(
    r"(?:what|which) (?P<entity>[a-z][a-z-]*(?: [a-z][a-z-]*){0,5}) "
    r"(?:is|was|does|did|has)\b[^?!]*\?",
    re.IGNORECASE,
)
_MISSING_ENTITY = re.compile(
    r"the (?:(?:provided|supplied|available|selected) )?(?:evidence|material|text|passage|paper) "
    r"does not (?:name|identify|specify|state|report|give|mention) "
    r"(?:(?:a|an|the|any) )?(?:(?:specific|exact) )?"
    r"(?P<entity>[a-z][a-z-]*(?: [a-z][a-z-]*){0,5})\.?",
    re.IGNORECASE,
)
_BACKGROUND = re.compile(
    r"(?:it|the (?:evidence|paper|text|passage)) only "
    r"(?:states|reports|says|mentions|notes|describes) (.+)",
    re.I,
)
_FINAL_DENIAL = re.compile(
    r"no (?P<entity>[a-z][a-z-]*(?: [a-z][a-z-]*){0,5}?)"
    r"(?: such as [^.;:!?]+)? (?:is|was) (?:named|identified|specified|reported|given|mentioned)\.",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_QUALIFIED_REQUEST = re.compile(
    r"\b(?:and|or|except|besides|other|additional|why|how|whether|second|also|else)\b|[,;:/]",
    re.I,
)


def explicit_abstention(query: str, answer: str) -> str | None:
    """Return the original refusal sentence only when the prose declines the complete request.

    This runs *after* validating all marker IDs and their correspondence to the
    selected evidence. Unrecognized wording, qualified answers, and other
    languages pass through. No entailment judgment is made about the evidence.
    """
    clean = _CITATION.sub("", answer).strip()
    if _WHOLE_ANSWER.fullmatch(clean) or _WHOLE_ANSWER_ZH.fullmatch(clean):
        return clean

    question = _SOURCE_PREFIX.sub("", query.strip())
    requested = _ENTITY_QUESTION.fullmatch(question)
    if not requested or _QUALIFIED_REQUEST.search(question):
        return None
    sentences = _SENTENCES.split(clean)
    missing = _MISSING_ENTITY.fullmatch(sentences[0])
    if not missing or missing["entity"].lower() != requested["entity"].lower():
        return None
    if len(sentences) == 1:
        return sentences[0]

    # Background between two explicit disclaimers is safe to omit only when
    # the author marks it as background and never mentions the requested noun
    # there. A contrast, separate assertion, or a direct partial answer proceeds
    # with its original citations. Require the two denials to refer to the same
    # singular entity; "No other ..." and other exceptions remain answers.
    if len(sentences) != 3 or not _BACKGROUND.fullmatch(sentences[1]):
        return None
    closing = _FINAL_DENIAL.fullmatch(sentences[2])
    entity = missing["entity"].lower()
    head = entity.split()[-1]
    if not closing or closing["entity"].lower() not in {head, entity}:
        return None
    if head.endswith("s") or re.search(
        rf"\b(?:{re.escape(head)}s?|but|however|although|except|instead|specifically)\b|[;:]",
        sentences[1],
        re.IGNORECASE,
    ):
        return None
    return sentences[0]
