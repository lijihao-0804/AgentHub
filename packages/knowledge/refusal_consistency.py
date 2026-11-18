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


def _missing_table_measure(question: str, clean: str) -> str | None:
    """Only discard a recap when every value belongs to a different table measure."""
    count = _TABLE_COUNT.fullmatch(question)
    if not count or _QUALIFIED_REQUEST.search(question[8:]):
        return None
    requested = count["measure"].casefold()
    if requested not in _TABLE_MEASURES:
        return None
    items = re.escape(count["items"])
    scope = re.escape(question[count.end("measure") : -1].strip())
    other = "|".join(key for key in _TABLE_MEASURES if key != requested)
    first, _, recap = clean.partition(". ")
    denial = (
        r"the (?:(?:provided|supplied) )?evidence does not (?:state|specify|provide|report) "
        rf"(?:{re.escape(question[:-1])}|the number of {items} {requested} {scope})"
    )
    if not re.fullmatch(denial, first.removesuffix("."), re.I):
        return None
    if not recap:
        return first.removesuffix(".") + "."
    body = re.fullmatch(
        rf"(?:it|the table) only (?:shows|reports) (.+), with no information about "
        rf"{_TABLE_MEASURES[requested]}\.",
        recap,
        re.I,
    )
    if not body:
        return None
    values = body[1].casefold()
    number = r"[0-9]+(?:,[0-9]{3})*"
    if values.startswith("that "):
        fields = values.removeprefix("that ").split(" and ")
        if all(
            re.fullmatch(rf"{number} (?:{items} )?(?:were|are) ({other})(?: {scope})?", f, re.I)
            for f in fields
        ):
            return first + "."
    labels = re.fullmatch(rf"([a-z ]+) figures for {items} {scope} \(([^()]+)\)", values, re.I)
    if labels:
        measures = set()
        for entry in labels[2].split("; "):
            cell = re.fullmatch(rf"({other}) {number}", entry)
            if not cell:
                return None
            measures.add(_TABLE_MEASURES[cell[1]])
        if measures == set(labels[1].split(" and ")):
            return first + "."
    return None


_ZH_NOUN = r"[\u3400-\u9fff]{1,36}"
_ZH_MULTI_OR_META = re.compile(
    r"[和与及、，,；;：:或]|以及|并且|分别|是否|有没有|哪些|多少.*多少|什么|哪[里个]|"
    r"提[到及]|说[明过]|记载|记录|报道|列出|原文|文中|有无|上下限|最高.*最低"
)
_ZH_NUMBER = re.compile(r"[零〇一二两三四五六七八九十百千万亿半0-9]+(?:[.点][0-9]+)?")
_ZH_BOUND = re.compile(
    r"至少|至多|超过|不足|接近|约有|大约|多[个间台套]|数[个间台套]|若干|许多|少量|大量|"
    r"多于|少于|不[到低高超过]|[高低大小]于|每|各|几[台个套间]|唯一|仅有|没有|不存在|"
    r"大致|介于|范围|相同|相等|一样|不变|升[温高]|下[降跌]|超[限标]|翻倍|为零|"
    r"免费|无偿|无|未|不|仅剩|只剩|用尽|售罄|告罄|清空|满额|全部|所有|均|皆|"
    r"多数|少数|成对|成双|相当|过半|接近|低于|高达"
)
_ZH_SOURCE = (
    r"(?:所提供的?|提供的|所给|给定|现有的?)?(?:证据|资料|材料|记录|报告|表格|文献|原文)(?:中)?"
)
_ZH_ABSENT = (
    r"(?:(?:不足以|无法|不能)(?:回答|确定|判断)|(?:未|没有|未能)(?:明确)?"
    r"(?:说明|指出|给出|列出|记录|报告|提供|标明))"
)
_ZH_SOURCE_INTRO = re.compile(
    rf"^(?:{_ZH_SOURCE})?(?:虽|仅|只|仅仅)?(?:记载|列出|报告|显示|提到|说明|指出|表明|描述|称)"
)


def _chinese_requested_value(question: str, clean: str, declared: bool) -> str | None:
    request = question.rstrip("？?")
    if _ZH_MULTI_OR_META.search(request):
        return None
    count = re.fullmatch(
        rf"({_ZH_NOUN})多少(?:[间个台套辆张名位户件种条座所次])?({_ZH_NOUN})", request
    )
    scalar = re.fullmatch(rf"({_ZH_NOUN})的({_ZH_NOUN})(?:是多少|为多少|有多[高长宽深重])", request)
    if not (count or scalar):
        return None
    first, _, rest = clean.partition("。")
    scope = request if count else f"{scalar[1]}的{scalar[2]}"
    generic = bool(declared and re.fullmatch(rf"{_ZH_SOURCE}{_ZH_ABSENT}(?:该|这个|此)问题", first))
    if not generic and not re.fullmatch(rf"{_ZH_SOURCE}{_ZH_ABSENT}{re.escape(scope)}", first):
        return None
    if not rest:
        return first + "。"
    if count:
        # A named inventory without a quantity may explain the gap. Require an
        # explicit list, the requested item, and an unqualified closing denial.
        inventory = re.fullmatch(
            rf"{_ZH_SOURCE}(?:仅|只)(?:提到|列出|说明|列举)([^。，；]+)，"
            rf"(?:但|却)?{_ZH_ABSENT}(?:具体|实际|确切)?(?:数量|数目|总数)。",
            rest,
        )
        if inventory and (declared or (count[2] in inventory[1] and "、" in inventory[1])):
            listed = inventory[1].replace(count[1], "").replace(count[2], "")
            if not (_ZH_NUMBER.search(listed) or _ZH_BOUND.search(listed)):
                return first + "。"
        if declared:
            context, _, closing = rest.rstrip("。").rpartition("，")
            if (
                _ZH_SOURCE_INTRO.match(context)
                and not _ZH_NUMBER.search(context)
                and not _ZH_BOUND.search(context)
                and re.fullmatch(
                    rf"(?:但|却)?{_ZH_ABSENT}[^，。；]{{0,32}}(?:数量|数字|数目|总数)", closing
                )
            ):
                return first + "。"
        return None
    if not declared:
        return None
    owner, attribute = scalar.groups()
    for sentence in re.split(r"[。；;]", rest):
        if not sentence:
            continue
        statement, contrast, excluded = sentence.partition("，并非")
        if contrast:
            # A numeric observation about another explicitly distinguished subject.
            if not (
                excluded == owner
                and owner not in statement
                and attribute in statement
                and _ZH_SOURCE_INTRO.match(statement)
                and len(_ZH_NUMBER.findall(statement)) == 1
                and not re.search(r"[，、]|而|又|也|但|同时|以及", statement)
            ):
                return None
            continue
        for clause in sentence.split("，"):
            clause = clause.removeprefix("但")
            if re.fullmatch(
                rf"{_ZH_ABSENT}(?:其|该)?(?:{re.escape(attribute)})(?:的)?(?:数值|数据|值)?", clause
            ):
                continue
            # Reported background with no asserted measurement, comparison or bound.
            if (
                not _ZH_SOURCE_INTRO.match(clause)
                or re.search(rf"{re.escape(attribute)}(?!计|仪|传感器|记录器)", clause)
                or _ZH_NUMBER.search(clause)
                or _ZH_BOUND.search(clause)
            ):
                return None
    return first + "。"


_EN_GAP = re.compile(
    r"(?:(?:the )?(?:(?:supplied|provided|available|selected) )?"
    r"(?:evidence|report|document|material|text|table|sources?|it|they) "
    r"(?:does not|do not|doesn't|cannot|fails to|is insufficient to) "
    r"|none of the (?:(?:supplied|provided|available) )?(?:evidence|sources|excerpts) )"
    r"(?:states?|specif(?:y|ies)|names?|identif(?:y|ies)|reports?|gives?|determine|establish|"
    r"provides?|records?|says?|mentions?) (.+)",
    re.I,
)
_EN_FUNCTION = frozenset(
    "in at as of on the a an to from who what which is was were are has have had did does do "
    "this that it s how many for her his their its".split()
)
_EN_QUANTITY_CLUE = re.compile(
    r"\d|[%$£€]|\b(?:no|not|none|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"single|only|unique|several|numerous|multiple|no[- ]one|more|most|less|fewer|many|few|"
    r"dozen|hundred|thousand|million|billion|half|twice|double|triple|fraction|some|all|"
    r"each|every|both|neither|higher|lower|equal|minimum|maximum|range|roughly|around|"
    r"estimate|same|majority|minority|free|unchanged|increase[ds]?|decrease[ds]?|"
    r"pair|couple|sole|nothing|large|small|plenty|enough|approximately|bounded|lesser|greater)\b",
    re.I,
)


def _en_terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+|\d+", text.casefold())) - _EN_FUNCTION


_BIBLIO_FIELDS = {
    "title": {"title", "titles"},
    "author": {"author", "authors", "writer", "writers"},
    "publisher": {"publisher", "publishers"},
    "date": {"date", "dates", "publication date", "release date"},
    "subject": {"subject", "subjects", "topic", "topics", "subject matter"},
}


def _only_other_fields(question: str, clauses: list[str]) -> bool:
    """A list of other named metadata fields supplies no value for the requested field."""
    target = re.match(r"(?:what|which) (?:is |was )?(?:the )?([a-z]+)\b", question, re.I)
    if not target or target[1].casefold() not in _BIBLIO_FIELDS:
        return False
    other = set().union(*(v for k, v in _BIBLIO_FIELDS.items() if k != target[1].casefold()))
    for clause in clauses:
        if _WHOLE_ANSWER.fullmatch(re.sub(r"^therefore, ", "", clause, flags=re.I)):
            continue
        note = re.fullmatch(
            r"(?:it|the (?:report|catalog|record|evidence|text)) (?:only |merely )?"
            r"(?:lists|describes|mentions|names|gives)(?: only)? (.+)",
            clause,
            re.I,
        )
        if not note:
            return False
        fields = re.split(r", (?:and )?| and ", note[1].casefold())
        if not all(re.sub(r"^(?:its|the|their) ", "", field) in other for field in fields):
            return False
    return True


def _english_requested_slot(question: str, clean: str, declared: bool) -> str | None:
    """Bind a missing predicate to one wh-question and account for every other clause."""
    if not re.fullmatch(
        r"(?:(?:in|during|as of) \d{4}, )?(?:who|what|which|how many|how much)\b[^?;:]+\?",
        question,
        re.I,
    ) or re.search(
        r"\b(?:and|or|except|also|else|whether|mentions?|reports?|states?|says?|documents?|"
        r"according|besides|exactly|approximately|excluding)\b",
        question,
        re.I,
    ):
        return None
    # E.g. the country where a group lives, or the company that built an object.
    # The relative clause identifies an intermediate entity rather than another request.
    focus, *bridge = re.split(
        r" (?:of )?the [\w-]+ (?:where|whose|that|which) ", question, flags=re.I
    )
    if len(bridge) > 1:
        return None
    clauses = re.split(r"\.\s+|,\s+(?:but|however|yet)\s+|;\s*", clean.rstrip("."), flags=re.I)
    denials = [(part, gap[1]) for part in clauses if (gap := _EN_GAP.fullmatch(part))]
    if len(denials) != 1:
        return None
    denial, missing = denials[0]
    wanted = _en_terms(focus)
    if not wanted or not wanted <= _en_terms(missing):
        return None
    # An elaboration inside a denial can supply a lower bound, candidate or a
    # qualified answer. Accept only the missing predicate itself.
    if re.search(
        r"[,;:!?]|\b(?:and|but|though|although|except|however|possibly|only|exact|"
        r"precise|least|most|approximately|probably|instead|additional|other|also|second)\b",
        missing,
        re.I,
    ):
        return None
    if not declared and missing.casefold() != question[:-1].casefold():
        return None
    if not bridge and (_en_terms(missing) - {"or"}) != wanted:
        return None
    if not bridge:
        if len(clauses) == 1 and not re.match(r"^(?:it|they)\b", denial, re.I):
            return denial + "."
        remaining = [c for c in clauses if c != denial]
        if declared and _only_other_fields(question, remaining):
            return denial + "."
        if declared and re.search(
            r"^how (?:many|much)\b|^what (?:is |was )?(?:the )?(?:cost|price|amount|count|"
            r"distance|duration|weight|length|width|temperature|volume)\b",
            question,
            re.I,
        ):
            for part in remaining:
                note = re.fullmatch(
                    r"(?:it|the (?:evidence|paper|report|source|text|passage|table)) "
                    r"(?:only |merely )?(?:states|shows|reports|describes|notes|mentions|"
                    r"identifies|lists)(?: only)? (?:that )?(.+)",
                    part,
                    re.I,
                )
                if not note or _EN_QUANTITY_CLUE.search(note[1]):
                    return None
            return denial + "."
        return None
    if not declared:
        return None
    # The resolved entity must occur in both the bridge assertion and the gap.
    # Retain only a standalone refusal, since naming that entity needs a citation.
    remainder = [p for p in clauses if p != denial]
    whole = [
        p for p in remainder if _WHOLE_ANSWER.fullmatch(re.sub(r"^therefore, ", "", p, flags=re.I))
    ]
    bridge_terms = _en_terms(bridge[0])
    bridges = [
        (part, match[1])
        for part in remainder
        if (
            match := re.fullmatch(
                r"the (?:evidence|text|report|document) "
                r"(?:shows|states|indicates|reports|says) that "
                r"[\w '-]+ (?:in|to|of|by|at|with|for) ([\w '-]+)",
                part,
                re.I,
            )
        )
        and bridge_terms <= _en_terms(part)
        and not re.search(r"\b(?:and|but|also|except|approximately|probably)\b", part, re.I)
    ]
    if len(whole) > 1 or len(bridges) != 1:
        return None
    bridge_statement, pivot = bridges[0]
    if not re.search(rf"\b{re.escape(pivot)}\b", missing, re.I):
        return None
    for part in remainder:
        if part in whole or part == bridge_statement:
            continue
        # The only other referenced observation expressly concerns a different
        # entity. An actual value for the pivot, or a conditional, must survive.
        if not re.fullmatch(
            rf"(?:the )?only [\w -]+ (?:mentioned|reported|listed) "
            rf"(?:concerns|relates to|is for) [\w '-]+, not {re.escape(pivot)}",
            part,
            re.I,
        ) or re.search(r"\b(?:and|but|also|except|probably)\b", part, re.I):
            return None
    return "The evidence is insufficient to answer the question."


_SLOT_WORDS = (
    ("when date dates dated year years", "哪一年|何年|哪天|日期|年份|何时|什么时候"),
    ("time times timing hour hours", "几点|时间|时刻"),
    ("cost price amount fee paid pay charge", "价格|费用|金额|多少钱"),
    ("count number many quantity total", "多少|数量|总数|数目"),
    ("where location place site", "哪里|何处|地点|位置"),
    ("who name identity person", "谁|姓名|人名"),
    ("title titled", "标题|题名|书名"),
)


def _only_missing_information(question: str, clean: str) -> str | None:
    """Each clause must be an absence of the requested field, or a bare source pointer."""
    chinese = bool(re.search(r"[\u3400-\u9fff]", question))
    if chinese:
        if _ZH_MULTI_OR_META.search(question) or not re.search(r"哪|谁|何|什么|多少|几", question):
            return None
        clauses = re.split(r"[。；！？]", clean.rstrip("。！？"))
    else:
        if not re.match(
            r"^(?:who|what|which|where|when|how many|how much|on what|at what)\b", question, re.I
        ) or re.search(
            r"\b(?:and|or|also|whether|except|omit|say|says|mention|mentions|lacking|wording|"
            r"quote|verbatim|heading|phrase|prior|before|after|until|since|between|"
            r"versus|compared|information is missing)\b|[;:]",
            question,
            re.I,
        ):
            return None
        clauses = re.split(r"(?<=[.!?])\s+|;\s*", clean.rstrip(".!?"))
    denial = None
    for clause in clauses:
        clause = clause.strip(" .!?。！？")
        if re.fullmatch(
            r"(?:see|refer to) (?:the )?(?:table|figure|section|appendix|chapter) [A-Z\d.]+"
            r"|(?:参见|见)(?:附表|表|图|附录|第)[一二三四五六七八九十A-Z\d.]+(?:章|节)?",
            clause,
            re.I,
        ) or explicit_abstention(question, clause, contextual=False):
            continue
        if chinese:
            gap = re.fullmatch(
                rf"{_ZH_SOURCE}{_ZH_ABSENT}([^，。；！？：]+)|([^，。；！？：]+?)"
                r"(?:未被|未予|尚未|未)(?:记录|列出|报告|注明|给出|提供)",
                clause,
            )
            if not gap:
                return None
            field = gap[1] or gap[2]
            field = re.sub(r"^(?:该|其|具体的?|确切的?)", "", field)
            if not field or not (
                field in question
                or any(re.fullmatch(zh, field) and re.search(zh, question) for _, zh in _SLOT_WORDS)
            ):
                return None
        else:
            gap = (
                _EN_GAP.fullmatch(clause)
                or re.fullmatch(
                    r"(?:the )?(?:(?:provided|supplied|available) )?"
                    r"(?:report|text|evidence|source|record|document|paper) "
                    r"(?:contains|gives|provides|lists|has) no (?:information (?:on|about) )?(.+)",
                    clause,
                    re.I,
                )
                or re.fullmatch(
                    r"(.+?) (?:is|was|are|were) (?:not|never) "
                    r"(?:specified|recorded|named|stated|listed|reported|given|provided)"
                    r"(?: in the (?:(?:supplied|provided) )?(?:report|evidence|source|record))?",
                    clause,
                    re.I,
                )
            )
            if not gap or re.search(
                r"[^A-Za-z0-9 '\u2019-]|\b(?:that|but|except|only|possibly|approximately|"
                r"however|though|not|never|and|or|least|most|than|additional|other)\b",
                gap[1],
                re.I,
            ):
                return None
            requested = _en_terms(question)
            field = _en_terms(gap[1]) - {"specific", "exact", "any"}
            # The absence may omit question context, but cannot introduce an
            # alternate entity, time, qualifier, or a new assertion.
            expanded = requested.copy()
            expanded.update(f"{word}ing" for word in requested)
            focus = set()
            for en, _ in _SLOT_WORDS:
                synonyms = set(en.split())
                if (requested | {question.split()[0].lower()}) & synonyms:
                    expanded.update(synonyms)
                    focus.update(synonyms)
            if not field or not field <= expanded or (not field & focus and field != requested):
                return None
        if denial is None:
            denial = clause + ("。" if chinese else ".")
    return denial


def first_whole_refusal(query: str, answer: str, *, declared: bool) -> str | None:
    """Recognize a complete information gap. Caller must first validate references."""
    question, clean = query.strip(), _CITATION.sub("", answer).strip()
    if re.search(
        r"\b(?:unreported|absent|missing|omitted|quote|quotation|reply|deny|denied)\b|"
        r"\b(?:does|did|is|was|are|were|has|have|can|could)\b.{0,35}"
        r"\b(?:mention|report|specify|list|name|state|say|quoted)\b|"
        r"是否|有无|有没有|没有(?:提及|说明|报告)|(?:未|没)(?:提及|提到|报告)的",
        question,
        re.I,
    ):
        return None
    if declared and not (
        _ZH_MULTI_OR_META.search(question)
        or re.search(
            r"\b(?:report|evidence|source|mention|state|say|document)\w*\b", question, re.I
        )
    ):
        pure = explicit_abstention(question, clean, contextual=False)
        if pure:
            return pure
    if declared and (direct := _only_missing_information(question, clean)):
        return direct
    return (
        echoed_count_refusal(question, answer)
        or _missing_table_measure(question, clean)
        or _chinese_requested_value(question, clean, declared)
        or _english_requested_slot(question, clean, declared)
    )


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
