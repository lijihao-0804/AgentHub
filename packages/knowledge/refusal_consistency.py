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


_AVERAGE_QUERY = re.compile(
    r"on average, by how much (?:do|did|does) [\w-]+ reduce (?:the )?"
    r"(?P<metric>[a-z-]+(?: [a-z-]+){0,4})\?",
    re.I,
)
_AVERAGE_MISSING = re.compile(
    r"the (?:provided|supplied) evidence does not (?:contain|give|provide) "
    r"a specific figure for the average reduction in (?P<metric>[a-z-]+(?: [a-z-]+){0,5})\.",
    re.I,
)
_NO_AVERAGE = re.compile(
    r"no (?:numeric|numerical|quantitative) (?:average|mean) (?:reduction|decrease) "
    r"(?:is|was) (?:reported|given|provided|specified)"
    r"(?: in the (?:(?:supplied|provided|available) )?(?:excerpts|evidence|text|paper))?\.",
    re.I,
)
_NUMBER_OR_BOUND = re.compile(
    r"\d|%|\b(?:percent|half|quarter|twice|double|one|two|three|four|five|six|seven|"
    r"eight|nine|ten|hundred|thousand|least|most|between|approximately|roughly|but|except)\b",
    re.I,
)

# Count questions often elicit a table recap even when the requested column is
# missing. Only discard a recap whose *entire* content enumerates different
# named measures. A bound, a partial count or unknown vocabulary stays intact.
_TABLE_MEASURES = {
    "produced": "production",
    "sold": "sales",
    "returned": "returns",
    "shipped": "shipments",
    "ordered": "orders",
}
_TABLE_COUNT = re.compile(
    r"how many (?P<items>[\w -]{1,100}?) (?:are|were|was|is) (?P<measure>[a-z]+) "
    r"(?:on|in|at|from|to) (?P<place>[\w -]{1,100})\?",
    re.I,
)
_COUNT_DENIAL = re.compile(
    r"the (?:(?:provided|supplied|available) )?(?:evidence|table|data|text) "
    r"(?:does not|doesn't) (?:report|specify|give|state|show) (.+)\.",
    re.I,
)
_TABLE_RECAP = re.compile(
    r"the table only (?:gives|lists|shows) (?P<measures>[a-z]+(?: and [a-z]+)*) "
    r"(?:figures|counts) for (?P<items>[\w -]{1,100}?)\s+[—–]\s+(?P<cells>.+?)\s+[—–]\s+"
    r"but no (?P<measure>[a-z]+) (?:quantity|count|total) is (?:stated|given|reported)"
    r"(?: anywhere)? in the (?:(?:supplied|provided|available) )?(?:evidence|table|data)\.",
    re.I,
)
_LABELED_COUNT = re.compile(r"([\w -]{1,100}?)\s*/\s*([a-z]+):\s*[0-9]+(?:,[0-9]{3})*", re.I)


def echoed_count_refusal(query: str, answer: str) -> str | None:
    """A single count explicitly declined in full, optionally with other table measures."""
    question = query.strip()
    count = _TABLE_COUNT.fullmatch(question)
    if (
        not count
        or _QUALIFIED_REQUEST.search(question[8:])
        or re.search(
            r"\b(?:not|excluding|including|within|before|after|versus|compared)\b", question, re.I
        )
    ):
        return None
    first, *rest = _SENTENCES.split(_CITATION.sub("", answer).strip())
    denial = _COUNT_DENIAL.fullmatch(first)
    if not denial or denial[1].casefold() != question[:-1].casefold():
        return None
    if not rest:
        return first
    wanted = count["measure"].casefold()
    if len(rest) != 1 or wanted not in _TABLE_MEASURES:
        return None
    summary = rest[0].casefold()
    other_fields = set(_TABLE_MEASURES.values()) - {_TABLE_MEASURES[wanted]}
    named = re.fullmatch(
        r"only ([a-z]+(?: and [a-z]+)*) (?:is|are) (?:reported|listed|given)\.", summary
    )
    if named:
        return first if set(named[1].split(" and ")) <= other_fields else None
    listed = re.fullmatch(r"(?:it|the table) (?:only )?(?:lists|shows|gives) (.+)\.", summary)
    if listed and all(
        (pair := re.fullmatch(r"[0-9]+(?:,[0-9]{3})* ([a-z]+)", term))
        and _TABLE_MEASURES.get(pair[1]) in other_fields
        for term in listed[1].split(" and ")
    ):
        return first
    aside = _TABLE_RECAP.fullmatch(rest[0])
    if (
        not aside
        or aside["measure"].casefold() != wanted
        or aside["items"].casefold() != count["items"].casefold()
    ):
        return None
    fields = set()
    for cell in re.split(r" and ", aside["cells"], flags=re.I):
        parsed = _LABELED_COUNT.fullmatch(cell)
        if not parsed or parsed[1].casefold() != count["place"].casefold():
            return None
        field = parsed[2].casefold()
        if field not in _TABLE_MEASURES or field == wanted:
            return None
        fields.add(_TABLE_MEASURES[field])
    return first if fields == set(aside["measures"].casefold().split(" and ")) else None


def explicit_abstention(query: str, answer: str, *, contextual: bool = True) -> str | None:
    """Return the original refusal sentence only when the prose declines the complete request.

    This runs *after* validating all marker IDs and their correspondence to the
    selected evidence. Unrecognized wording, qualified answers, and other
    languages pass through. No entailment judgment is made about the evidence.
    """
    clean = _CITATION.sub("", answer).strip()
    if _WHOLE_ANSWER.fullmatch(clean) or _WHOLE_ANSWER_ZH.fullmatch(clean):
        return clean

    if not contextual:
        return None
    question = _SOURCE_PREFIX.sub("", query.strip())
    sentences = _SENTENCES.split(clean)
    wanted = _AVERAGE_QUERY.fullmatch(question)
    if wanted and len(sentences) == 3 and not _QUALIFIED_REQUEST.search(wanted["metric"]):
        missing_metric = _AVERAGE_MISSING.fullmatch(sentences[0])
        metric = wanted["metric"].lower()
        if (
            missing_metric
            and missing_metric["metric"].lower() in {metric, metric + " rate"}
            and sentences[1].lower().startswith("it only states qualitatively that ")
            and not _NUMBER_OR_BOUND.search(sentences[1])
            and _NO_AVERAGE.fullmatch(sentences[2])
        ):
            return sentences[0]
    requested = _ENTITY_QUESTION.fullmatch(question)
    if not requested or _QUALIFIED_REQUEST.search(question):
        return None
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


# Omitting a document-navigation aside is safe only for a single information
# request when the aside merely locates a section and repeats the same gap.
_WH_REQUEST = re.compile(r"(?:what|which|how|why|when|where|who)\b[^?;:]+\?", re.I)
_SCOPE_DENIAL = re.compile(
    r"The (?:(?:provided|available|supplied) )?evidence does not "
    r"(?:specify|describe|state|explain|identify) (?P<requested>[^.!?;:]+)\.",
    re.I,
)
_NAVIGATION_ASIDE = re.compile(
    r"The (?:paper|article|report) (?:mentions|states|notes) that "
    r"(?:the )?(?:results|findings|details) are (?:shown|presented|reported)"
    r"(?: and evaluated)? in (?:section|chapter|appendix) [A-Z0-9]+(?:\.\d+)*"
    r"(?: \[\d+\])+, but the (?:evidence )?(?:excerpts|passages) "
    r"do not include that (?:section|chapter|appendix) or (?:name|describe|give) "
    r"any (?P<missing>[a-z]+(?: [a-z]+){0,3})\.",
    re.I,
)


def recover_whole_refusal(query: str, answer: str) -> tuple[str, str] | None:
    """Return only a pure refusal or an omission containing just a source pointer."""
    pure = explicit_abstention(query, answer, contextual=False)
    if pure:
        return pure, "pure_whole_refusal"
    question = _SOURCE_PREFIX.sub("", query.strip()).casefold()
    if not _WH_REQUEST.fullmatch(question) or re.search(
        r"\b(?:and|or|besides|except|additional|other|also|section|chapter|appendix|where)\b|[,/]",
        question,
    ):
        return None
    sentences = _SENTENCES.split(answer.strip())
    if len(sentences) != 2:
        return None
    denial = next((m for part in sentences if (m := _SCOPE_DENIAL.fullmatch(part))), None)
    aside = next((m for part in sentences if (m := _NAVIGATION_ASIDE.fullmatch(part))), None)
    if not denial or not aside:
        return None
    head = aside["missing"].split()[-1].casefold()
    if not all(
        re.search(rf"\b{re.escape(head)}\b", s, re.I) for s in (question, denial["requested"])
    ):
        return None
    if re.search(r"\b(?:except|but|only|other|additional|besides|although)\b", denial[0], re.I):
        return None
    return denial[0], "omitted_document_navigation_only"


def refuses_requested_comparison(query: str, answer: str) -> bool:
    """Recognize an explicit denial of the requested two-sided comparison.

    Do not erase the cited single-sided explanation. The public refusal shape
    cannot carry its attribution. A new first response must resolve the conflict.
    """
    question = _SOURCE_PREFIX.sub("", query.strip())
    questions = (
        r"how (?:does|did|do) [\w -]+ differ from (?P<t>[\w -]+)",
        r"(?:what|which) (?:is|are|were) the differences? between [\w -]+? and (?P<t>[\w -]+)",
        r"(?:compare [\w -]+? (?:with|to)|how (?:does|did) [\w -]+? compare (?:with|to)) "
        r"(?P<t>[\w -]+)",
    )
    chinese = re.fullmatch(
        r"(?:请比较|比较)([^和与，。？！；]+)[和与]([^，。？！；]+?)(?:的差异|的区别)?[？?。]?",
        question,
    )
    if chinese:
        target = chinese[2]
        parts = [s.strip() for s in re.split("[。！？]", answer) if s.strip()]
        denials = [
            s
            for s in parts
            if re.fullmatch(
                rf"(?:现有|所给|提供的)?(?:证据|资料|材料)(?:没有|未)(?:提供|给出|说明)"
                rf"(?:二者的比较|二者的区别|两者的差异|与{re.escape(target)}的比较)",
                s,
            )
        ]
        facts = [s for s in parts if s not in denials]
        return bool(
            denials
            and facts
            and all(
                re.match(r"(?:材料|资料|证据|文中)(?:只|仅)(?:说明|指出|提到|描述)", s)
                and not any(
                    token in s
                    for token in (
                        target,
                        "相比",
                        "差异",
                        "区别",
                        "不同",
                        "相同",
                        "均",
                        "更",
                        "而",
                        "但是",
                    )
                )
                for s in facts
            )
        )
    query_text = question.rstrip("?.").strip()
    match = next(
        (m for pattern in questions if (m := re.fullmatch(pattern, query_text, re.I))), None
    )
    if not match or re.search(r"\b(?:also|except|additional|besides|or)\b", query_text, re.I):
        return False
    if re.search(r"\band\b", query_text, re.I) and (
        "between " not in query_text.casefold()
        or len(re.findall(r"\band\b", query_text, re.I)) != 1
    ):
        return False
    target = match["t"].strip().casefold()
    initials = "".join(w[0] for w in target.split())
    names = {target}
    if len(initials) > 1:
        names.update((initials, initials + "s"))
    targets = "|".join(re.escape(n) for n in names)
    denial = re.compile(
        rf"(?:the (?:(?:supplied|provided|available) )?(?:evidence|material|text) does not "
        rf"(?:describe how [\w -]+ differs from|compare [\w -]+ (?:with|to)) (?:{targets})"
        rf"|no (?:direct )?comparison (?:to|with) (?:{targets}) is "
        rf"(?:given|provided|available)(?: in the supplied evidence)?)",
        re.I,
    )
    sentences = [part.removesuffix(".") for part in _SENTENCES.split(answer.strip())]
    facts = [part for part in sentences if not denial.fullmatch(part)]
    if not facts or len(facts) == len(sentences):
        return False
    contrast = re.compile(
        rf"\b(?:{targets}|unlike|whereas|than|but|however|both|contrast|different|difference|"
        rf"compared|except|instead|only one|neither|former|latter|respectively)\b",
        re.I,
    )
    return all(
        re.match(
            r"(?:it|the (?:paper|report|evidence|text)) only "
            r"(?:states|reports|describes|mentions) ",
            part,
            re.I,
        )
        and not contrast.search(part)
        for part in facts
    )
