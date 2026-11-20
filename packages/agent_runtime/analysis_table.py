"""Rebuild explicit Markdown tables from this turn's recorded warehouse reads."""

from __future__ import annotations

import html
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import date
from uuid import UUID

from packages.agent_runtime.work_layer import RecordedToolCall
from packages.artifacts.projections import project_analysis_query, tool_tail
from packages.artifacts.schemas import validate_artifact_content
from packages.core.errors.exceptions import AgentHubError

_ONLY = re.compile(
    r"(?:^|[\s。！？，、；.!?,;])(?:请)?(?:仅|只)(?:需要|要|需|给出|给|返回|输出|展示|显示)"
    r"(?:这[个张]|一张|一个)?\s*(?:markdown\s*)?表(?:格)?(?=$|[\s。！？，；.!?,;])"
    r"|(?:表格|一张表)(?:即可|就行)(?=$|[\s。！？，；.!?,;])"
    r"|\b(?:table[- ]only|only (?:a |the )?(?:markdown )?table|(?:a |the )?table only)\b",
    re.I,
)
_EXPLANATION = re.compile(
    r"解释|说明|分析原因|为什么|\b(?:explain|explanation|interpretation|commentary)\b", re.I
)
_NO_EXPLANATION = re.compile(
    r"(?:不用|不要|无需|不需要|不必|不加|不附)(?:额外)?(?:解释|说明)"
    r"|\b(?:without|no) (?:any )?(?:explanation|commentary)\b",
    re.I,
)
# Only aliases whose meaning is defined by the corresponding typed tool.
_ORDER = {
    "cohort": (
        "用户分组",
        "分组",
        "用户组",
        "组别",
        "用户群组",
        "用户群体",
        "人群",
        "用户类型",
        "组",
    ),
    "users": ("去重人数", "去重用户数", "用户数", "人数"),
    "order_rows": ("全部订单数", "本组全部订单数", "本组订单数", "订单总数", "订单数"),
    "non_paid_rows": ("非 PAID 订单数", "本组非 PAID 订单数", "非 PAID 单数"),
    "paid_rows": ("PAID 订单数", "本组 PAID 订单数"),
    "refunded_rows": ("退款订单数", "退款单数"),
    "cancelled_rows": ("取消订单数", "取消单数"),
    "other_status_rows": ("其他状态订单数",),
}
_GROUPS = {
    "with_paid_order": (
        "窗口内有 PAID 订单",
        "窗口内有 PAID 订单的人",
        "有 PAID 订单",
        "有 PAID 订单的人",
        "有 PAID 订单的用户",
    ),
    "without_paid_order": (
        "窗口内没有 PAID 订单",
        "窗口内没有 PAID 订单的人",
        "无 PAID 订单",
        "无 PAID 订单的人",
        "无 PAID 订单的用户",
        "没有 PAID 订单",
    ),
}
_PAYING = {"measure": ("支付口径", "口径", "指标"), "users": ("人数", "用户数")}
_MEASURES = {
    "strict_first_qualified_users": ("首笔支付合格", "首笔合格"),
    "any_qualified_users": ("任意一次支付合格", "任意一次合格"),
}
_CONVERSION = {
    "scope": ("范围", "分组"),
    "platform": ("平台",),
    "app_version": ("应用版本", "版本"),
    "start_date": ("开始日期",),
    "end_date": ("结束日期",),
    "signups": ("注册数",),
    "views": ("浏览数",),
    "rate_pct": ("转化率",),
    "change_pp": ("变化百分点",),
}
_DATE = re.compile(r"(?<![0-9])\d{4}-\d{2}-\d{2}(?![0-9])")
_RANGE_END = re.compile(
    r"\s*(?:至|到|through\b|to\b|[–—~～]|\s-\s)\s*"
    r"(?P<month>\d{1,2})-(?P<day>\d{1,2})(?![\d-])",
    re.I,
)


def wants_warehouse_table(question: str) -> bool:
    return bool(
        _ONLY.search(question)
        and not _EXPLANATION.search(_NO_EXPLANATION.sub("", question))
        and not re.search(r"\b(?:not|never)\s+(?:a\s+)?(?:table.only|only)", question, re.I)
    )


def table_evidence(call: RecordedToolCall, run_id: UUID, *, v2: bool = False) -> dict | None:
    # Keep the bounded cells and identities only. SQL, provider prose, and
    # tool arguments are never interpolated into Markdown or instructions.
    artifact = project_analysis_query(call, run_id)
    if artifact is None:
        return None
    try:
        content = validate_artifact_content(artifact.type, artifact.content)
    except AgentHubError:
        return None
    rows = content["rows"]
    if content["truncated"] or content["row_count"] != len(rows) or (not rows and not v2):
        return None
    columns = content["columns"]
    if any(not _plain_cell(c) for c in columns) or any(
        not _plain_cell(v) for row in rows for v in row
    ):
        return None
    if len({_key(c) for c in columns}) != len(columns):
        return None
    name = tool_tail(call.tool_identity)
    args = content.get("metric_arguments", {})
    windows = [args] if name == "get_order_activity" else args.get("windows", [])
    result = {
        "tool": name,
        "columns": columns,
        "rows": rows,
        "dates": [w[key] for w in windows for key in ("start_date", "end_date")],
    }
    if v2:
        result["call_id"] = call.tool_call_id
        result["identity"] = call.tool_identity
    return result if len(json.dumps(result, ensure_ascii=False)) <= 32_768 else None


def _plain_cell(value) -> bool:
    if isinstance(value, str):
        return len(value) <= 500 and not any(
            unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"} for c in value
        )
    return (
        value is None or type(value) in (bool, int) or type(value) is float and math.isfinite(value)
    )


def _key(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _cell(value) -> str:
    if value is None:
        return "NULL"
    return str(value)


def _safe(value: str) -> str:
    # GFM Markdown punctuation is literal after escaping. Escape the HTML
    # characters before Markdown, and keep each cell on its original line.
    value = html.escape(value, quote=True).replace(":", "&#58;").replace("@", "&#64;")
    return re.sub(r"([\\|`*_~\[\]()!])", r"\\\1", value)


def _parse_table(content: str) -> list[list[str]] | None:
    """Select exactly one contiguous GFM pipe table from surrounding prose."""
    lines = content.splitlines()

    def parts(line):
        body = line.strip().removeprefix("|")
        if body.endswith("|") and not body.endswith("\\|"):
            body = body[:-1]
        return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]

    dividers = [
        i
        for i, line in enumerate(lines)
        if "|" in line and all(re.fullmatch(r":?-{3,}:?", c) for c in parts(line))
    ]
    if len(dividers) != 1 or dividers[0] == 0:
        return None
    i = dividers[0]
    if "|" not in lines[i - 1]:
        return None
    header = parts(lines[i - 1])
    if len(header) != len(parts(lines[i])) or not all(header):
        return None
    table = [header]
    for line in lines[i + 1 :]:
        if "|" not in line:
            break
        row = parts(line)
        if len(row) != len(header):
            return None
        table.append(row)
    return table if len(table) > 1 else None


def _question_dates(question: str) -> list[str] | None:
    dates = []
    for match in _DATE.finditer(question):
        try:
            start = date.fromisoformat(match[0])
            dates.append(start.isoformat())
            end = _RANGE_END.match(question, match.end())
            if end:
                month, day = int(end["month"]), int(end["day"])
                finish = date(start.year + (month < start.month), month, day)
                if finish < start:
                    return None
                dates.append(finish.isoformat())
        except ValueError:
            return None
    return dates


def _window_matches(source, question, *, expand_ranges=False):
    name = source["tool"]
    if name == "query_sql":
        return True
    dates = _question_dates(question) if expand_ranges else _DATE.findall(question)
    if dates is None:
        return False
    if (
        name == "get_order_activity"
        and dates
        and source["dates"] != (dates * 2 if len(dates) == 1 else dates)
    ):
        return False
    if name == "get_registration_conversion" and dates and set(dates) != set(source["dates"]):
        return False
    if name == "get_paying_user_counts" and dates:
        return False
    return True


def _aliases(name):
    return {
        "get_order_activity": _ORDER,
        "get_paying_user_counts": _PAYING,
        "get_registration_conversion": _CONVERSION,
    }.get(name, {})


def _valid_columns(name, question, columns, indices):
    if name == "get_order_activity":
        # A table may contain only true cells and still omit the requested
        # measure, or assign counts to unnamed cohorts.
        normalized = _key(question)
        terms = sorted(
            (
                (_key(alias), field)
                for field, aliases in _ORDER.items()
                if field != "cohort"
                for alias in (field, *aliases)
            ),
            key=lambda entry: -len(entry[0]),
        )
        by_term = dict(terms)
        requested = {
            by_term[m.group()] for m in re.finditer("|".join(map(re.escape, by_term)), normalized)
        }
        present = {columns[i] for i in indices}
        if "cohort" not in present or (requested and not requested <= present):
            return False
    return True


def _match(table, source, question, *, translated=False):
    name = source["tool"]
    if not _window_matches(source, question, expand_ranges=translated):
        return None
    aliases = _aliases(name)
    columns = source["columns"]
    indices = []
    for position, header in enumerate(table[0]):
        matches = [
            i
            for i, c in enumerate(columns)
            if _key(header) in {_key(item) for item in (c, *aliases.get(c, ()))}
        ]
        if not matches and translated and len(table) - 1 == len(source["rows"]):
            # SQL column names are arbitrary. Data can identify a translation
            # only if precisely one whole source column has those values.
            proposed = Counter(row[position] for row in table[1:])
            matches = [
                i
                for i in range(len(columns))
                if proposed == Counter(_cell(row[i]) for row in source["rows"])
                or proposed == Counter(_safe(_cell(row[i])) for row in source["rows"])
            ]
        if len(matches) != 1 or matches[0] in indices:
            return None
        indices.append(matches[0])
    if not _valid_columns(name, question, columns, indices):
        return None
    lookup = {}
    for row in source["rows"]:
        cells = [_cell(row[i]) for i in indices]
        # Match each cell under its own column; matching independent values
        # from different groups would validate a transposed or fabricated row.
        lookup[tuple(cells)] = lookup.get(tuple(cells), 0) + 1
    values = (
        _GROUPS
        if name == "get_order_activity"
        else _MEASURES
        if name == "get_paying_user_counts"
        else {}
    )
    selected = []
    for proposed in table[1:]:
        choices = []
        for row, remaining in lookup.items():
            if remaining and all(
                cell == raw
                or _safe(raw) == cell
                or (
                    columns[i] in {"cohort", "measure"}
                    and any(_key(cell) == _key(a) for a in values.get(raw, ()))
                )
                for cell, raw, i in zip(proposed, row, indices, strict=True)
            ):
                choices.append(row)
        if len(choices) != 1:
            return None
        match = choices[0]
        lookup[match] -= 1
        selected.append(
            [
                values[v][0] if columns[i] in {"cohort", "measure"} and v in values else v
                for v, i in zip(match, indices, strict=True)
            ]
        )
    if name in {"query_sql", "get_order_activity"} and any(lookup.values()):
        return None
    headers = [aliases.get(columns[i], (columns[i],))[0] for i in indices]
    rows = [headers, ["---"] * len(headers), *selected]
    return "\n".join("| " + " | ".join(_safe(v) for v in row) + " |" for row in rows)


def grounded_table(question: str, content: str, sources: list[dict]) -> str | None:
    table = _parse_table(content)
    if table is None:
        return None
    matches = {answer for s in sources if (answer := _match(table, s, question)) is not None}
    return matches.pop() if len(matches) == 1 else None


# The terminal message declares a projection. It cannot supply numbers, row
# contents, SQL, arbitrary labels, or explanatory prose to the renderer.
_SELECTION_RULES = """
For this table-only turn, query the needed data first. When a recorded result
answers the question, return one JSON object instead of copying its numbers:
{"source":"<current tool_call_id or unique tool name>","columns":["<real column key>"]}.
Use the real column keys in the requested order. SQL uses its returned columns.
Order activity uses cohort, users, order_rows, paid_rows, non_paid_rows,
refunded_rows, cancelled_rows, other_status_rows. The paying-users table uses
measure and users; optionally select its rows with "measures" (a list of keys:
strict_first_qualified_users, any_qualified_users, any_only_first_failed,
any_only_first_refunded, any_only_first_other). Registration conversion uses
scope, platform, app_version, start_date, end_date, signups, views, rate_pct,
change_pp; optionally set "scope" to overall, platform, or version. Use SQL if
the requested filters or dimensions do not match the metric tool. Do not select
exploratory results. If a prerequisite is missing, return {"clarify":"window"}
or use metric, grouping, or source. The runtime renders the user-visible table.
""".strip()

# Determine what the restriction applies to within its clause. "Only include
# Q2 orders" restricts the data; "only deliver a Q2 table" restricts the answer.
_TABLE_NOUN = re.compile(
    r"表格|单表|(?:一|这|该)(?:张|个)表|表(?=$|[\s即可就足够呈展显交输作结前后外，。！？；：、])"
    r"|\btables?\b",
    re.I,
)
_EXCLUSIVE = r"(?:仅(?:仅)?|只(?:需(?:要)?)?|就)"
_DELIVERY = (
    r"(?:交(?:付|给|出)?(?!易)|给(?:我|出)?|提(?:交|供)|输(?:出)?|返(?:回)?|"
    r"呈(?:现|交)?|展(?:示)?|显(?:示)?|列(?:出|示)?|发(?:送)?|留(?:下)?|保留|用|以|要|需要|需)"
)
# A following modifier can describe the contents or the shape of the table.
# A restriction on a filter/source followed by a second instruction cannot.
_ONLY_DELIVERY = re.compile(
    rf"{_EXCLUSIVE}\s*(?:{_DELIVERY}|看(?=(?:这|一|该)(?:张|个)表))"
    r"|(?:最终)?(?:答复|回答|交付物|输出|结果|格式)(?:限定|限于|限为|限|只允许|只能是|必须是)"
    r"|\b(?:only|just)\s+(?:(?:return|provide|deliver|give|show|output|send|display)\b|an?\b|the\b|tables?\b)"
    r"|\b(?:return|provide|deliver|give|show|output|send|display)\s+(?:only|just)\b",
    re.I,
)
_ONLY_OBJECT = re.compile(
    r"(?:仅(?:仅)?|只(?:需(?:要)?|要)?|就要)\s*(?:(?:这|该)?(?:一张|一个)?(?:单表|表格|markdown)|(?:这张|这个|一张|一个)表)",
    re.I,
)
_TABLE_SUFFICES = re.compile(
    r"(?:即可|就(?:行|好|够)|便可|足够|为限|为止|就可以)|\b(?:only|nothing else)\b", re.I
)
_NEGATED_FORMAT = re.compile(
    r"(?:不要|不必|不能|不应|不是|不想|别|不)(?:再)?(?:仅|只)|"
    r"\b(?:not|never|don't|do not)\s+(?:just|only|(?:a |the )?table.only)\b",
    re.I,
)
_QUOTES = re.compile(r'“[^”\n]*”|「[^」\n]*」|"[^"\n]*"|`[^`\n]*`')
_OUTSIDE_TABLE = re.compile(r"表(?:格)?(?:的)?(?:前(?:后)?|后|外|以外|之外)|表格?(?:上下|两侧)")
_NEGATION = (
    r"(?:不要|不需(?:要)?|无需|不用|不必|不得|禁止|勿|别|"
    r"不(?:写|加|附|带|要|给|作|放|留|输出)|省略|免去)"
)
_PROSE = (
    r"(?:文字|文本|说明|解释|解读|分析|原因|补充|附注|前言|后记|前缀|"
    r"后缀|总结|叙述|评论|旁白|内容|语句|句子)"
)
_NO_PROSE = re.compile(
    rf"{_NEGATION}(?:在表格?(?:前后|之前|之后|外|上方|下方))?"
    rf"(?:再|都|也|另行|另外|额外|添加|附加|附带|附上|附|插入|出现|输出|撰写|写|给出|加上|加|放|有|"
    rf"任何|多余|其他|其它|额外的|一段|一句|的|表外|表前|表后|开场|收尾|行文)*{_PROSE}"
    rf"|{_PROSE}(?:都|也)?(?:不用|不要|免了|不需要)"
    r"|\b(?:without|no|omit)\s+(?:any\s+)?(?:extra\s+|additional\s+|surrounding\s+)?"
    r"(?:prose|text|explanation|interpretation|commentary|introduction|conclusion)\b",
    re.I,
)
_ADDITIONAL = re.compile(
    r"解释|说明|解读|为什么|分析一下|(?:分析|讲讲|交代|说说)(?:其|一下|背后)?原因|"
    r"(?:再|另|并|还).{0,6}(?:画图|图形|图表|趋势图|写一段|补充一段|附上文字|总结)|"
    r"\b(?:explain|explanation|interpretation|commentary|why)\b",
    re.I,
)
_HYPOTHETICAL = re.compile(r"如果|假如|假设|\b(?:if|suppose)\b", re.I)


def table_only_turn(question: str, policy: str | None) -> bool:
    if policy == "grounded_table_v1":
        return wants_warehouse_table(question)
    if policy != "grounded_table_v2":
        return False
    text = _QUOTES.sub(" ", question)
    has_table = False
    exclusive = False
    forbids_prose = False
    for sentence in re.split(r"[。！？;；!?\n]", text):
        if _HYPOTHETICAL.search(sentence):
            continue
        for clause in re.split(r"[,，]", sentence):
            # Removing the negative span preserves an independently requested
            # explanation in the rest of the same clause.
            remainder = _NO_PROSE.sub("", clause)
            if _NEGATED_FORMAT.search(clause) or _ADDITIONAL.search(remainder):
                return False
            if (
                _OUTSIDE_TABLE.search(clause)
                and remainder == clause
                and re.search(r"写|附|加|放|留", clause)
            ):
                return False
            forbids_prose |= remainder != clause
            noun = _TABLE_NOUN.search(clause)
            if not noun:
                continue
            has_table = True
            prefix = clause[: noun.start()]
            suffix = clause[noun.end() :]
            delivery = _ONLY_DELIVERY.search(prefix)
            if delivery and re.search(
                r"然后|随后|最后再|再(?:将|用|把|做|整理)|and then", prefix[delivery.end() :], re.I
            ):
                delivery = None
            exclusive |= bool(
                delivery
                or _ONLY_OBJECT.search(clause)
                or _TABLE_SUFFICES.search(suffix)
                or re.search(r"\btable[- ]only\b", clause, re.I)
            )
    return has_table and (exclusive or forbids_prose)


def analysis_system_prompt(prompt: str, runtime: dict, question: str) -> str:
    policy = runtime.get("analysis_answer_policy")
    if policy == "grounded_table_v2" and table_only_turn(question, policy):
        return prompt + "\n\n" + _SELECTION_RULES
    return prompt


def _project_selection(question, selection, source):
    name = source["tool"]
    keys = selection.get("columns")
    allowed = {"source", "columns"}
    if name == "get_paying_user_counts":
        allowed.add("measures")
    elif name == "get_registration_conversion":
        allowed.add("scope")
    if set(selection) - allowed or not _window_matches(source, question, expand_ranges=True):
        return None
    columns = source["columns"]
    if (
        not isinstance(keys, list)
        or not keys
        or not all(isinstance(k, str) and k in columns for k in keys)
    ):
        return None
    if len(set(keys)) != len(keys):
        return None
    indices = [columns.index(k) for k in keys]
    if not _valid_columns(name, question, columns, indices):
        return None
    rows = source["rows"]
    if "measures" in selection:
        wanted = selection["measures"]
        if (
            not isinstance(wanted, list)
            or not wanted
            or not all(isinstance(k, str) for k in wanted)
        ):
            return None
        measure = columns.index("measure")
        by_key = {row[measure]: row for row in rows}
        if set(wanted) - by_key.keys() or len(wanted) != len(set(wanted)) or "measure" not in keys:
            return None
        rows = [by_key[k] for k in wanted]
    if "scope" in selection:
        scope = selection["scope"]
        if scope not in ("overall", "platform", "version"):
            return None
        rows = [row for row in rows if row[columns.index("scope")] == scope]
        if not rows:
            return None
    aliases = _aliases(name)
    labels = (
        _GROUPS
        if name == "get_order_activity"
        else _MEASURES
        if name == "get_paying_user_counts"
        else {}
    )
    headers = [aliases.get(k, (k,))[0] for k in keys]
    result = [headers, ["---"] * len(keys)]
    for row in rows:
        result.append(
            [
                labels.get(row[i], (row[i],))[0] if columns[i] in {"cohort", "measure"} else row[i]
                for i in indices
            ]
        )
    return "\n".join("| " + " | ".join(_safe(_cell(v)) for v in row) + " |" for row in result)


_CLARIFICATIONS = {
    "window": ("请提供查询的起止日期。", "Please specify the date range."),
    "metric": ("请明确指标和统计口径。", "Please specify the metric and its definition."),
    "grouping": ("请明确分组和需要的列。", "Please specify the grouping and requested columns."),
    "source": (
        "请明确要使用的查询结果或数据来源。",
        "Please identify the intended query result or data source.",
    ),
}


def _clarification(question, key):
    zh = bool(re.search(r"[\u3400-\u9fff]", question))
    title = "待确认" if zh else "Clarification needed"
    return f"| {title} |\n| --- |\n| {_CLARIFICATIONS[key][0 if zh else 1]} |"


def grounded_answer(question: str, content: str, sources: list[dict], policy: str) -> str | None:
    if policy != "grounded_table_v2":
        return grounded_table(question, content, sources)
    raw = content.strip()
    if raw.startswith("```json\n") and raw.endswith("\n```"):
        raw = raw[8:-4].strip()
    try:
        selection = json.loads(raw)
    except (ValueError, RecursionError):
        selection = None
    if isinstance(selection, dict):
        if (
            set(selection) == {"clarify"}
            and isinstance(selection["clarify"], str)
            and selection["clarify"] in _CLARIFICATIONS
        ):
            return _clarification(question, selection["clarify"])
        reference = selection.get("source")
        candidates = (
            [s for s in sources if reference in (s.get("call_id"), s.get("identity"), s["tool"])]
            if isinstance(reference, str)
            else []
        )
        if len(candidates) == 1:
            return _project_selection(question, selection, candidates[0])
        return _clarification(question, "source") if len(candidates) > 1 else None
    table = _parse_table(content)
    if table:
        matches = [
            answer
            for s in sources
            if (answer := _match(table, s, question, translated=True)) is not None
        ]
        if len(matches) == 1:
            return matches[0]
        return _clarification(question, "source") if len(matches) > 1 else None
    # A genuine request for more context can be shown without admitting any
    # unchecked model claims into the user-visible response.
    if "?" in content or "？" in content:
        for key, terms in (
            ("window", r"日期|时间|date|period|window"),
            ("grouping", r"分组|列|group|column"),
            ("metric", r"指标|口径|metric|definition"),
        ):
            if re.search(terms, content, re.I):
                return _clarification(question, key)
        return _clarification(question, "source")
    return None
