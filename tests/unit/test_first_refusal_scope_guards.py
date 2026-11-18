"""An explicit gap in one fact cannot erase a cited answer to another requested fact."""

import json

import pytest

from packages.knowledge.citation_qa import _InvalidCitationAnswer, resolve_citation_answer


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        (
            "How many cases were closed?",
            "The note does not give a closing date. Four cases were closed [1].",
        ),
        ("Were any samples lost?", "No samples were lost [1]. The log does not name the courier."),
        (
            "What is the recorded expiry date?",
            "The expiry field explicitly reads ‘not applicable’ [1].",
        ),
        (
            "Which rack is active, and when was it installed?",
            "The installation date is not stated. Rack K is active [1].",
        ),
        (
            "What bound is known for the number of returns?",
            "The exact total is not given, but at least six returns are documented [1].",
        ),
        (
            "Did the report identify the owner?",
            "No. The report expressly says the owner could not be identified [1].",
        ),
        (
            "当地周五关闭了多少个工位？",
            "材料未提供停用工位的总数，但明确写明周五关闭了两个工位[1]。",
        ),
        ("泵房使用哪个阀门，什么时候更换的？", "更换日期无法确定，正在使用北侧阀门[1]。"),
        ("清单有没有记录该产品的校准日期？", "没有。校准日期一栏明确标为“未记录”[1]。"),
        ("这批货抽检出多少件不合格品？", "未报告全仓缺陷数，本批抽检不合格品为零件[1]。"),
        ("引文里哪句话被当作反例？", "“现有证据不足以回答这个问题”正是反例中引用的原话[1]。"),
    ],
)
def test_evidenced_answer_and_qualifier_stay_together(question, answer):
    raw = json.dumps(dict(answer=answer, citation_ids=[1], insufficient_evidence=False))
    for enabled in (False, True):
        resolved = resolve_citation_answer(
            question,
            raw,
            visible_ids={1},
            output_limit_reached=False,
            first_refusal_contract=enabled,
        )
        assert (resolved.answer, resolved.insufficient_evidence, resolved.citation_ids) == (
            answer,
            False,
            (1,),
        )


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        (
            "Who signed the receipt and on what date?",
            "The supplied evidence does not state the date. Mira signed the receipt [1].",
        ),
        ("验收人是谁，费用是多少？", "现有证据不足以确定费用。验收人是穆青[1]。"),
        (
            "Who approved the shipment?",
            "The evidence is insufficient to determine who paid. Arden approved the shipment [1].",
        ),
    ],
)
def test_wrong_refusal_flag_cannot_hide_a_positive_fact(question, answer):
    raw = json.dumps(dict(answer=answer, citation_ids=[1], insufficient_evidence=True))
    try:
        resolved = resolve_citation_answer(
            question, raw, visible_ids={1}, output_limit_reached=False, first_refusal_contract=True
        )
    except _InvalidCitationAnswer:
        return  # The product may send its ordinary citation correction.
    assert not resolved.insufficient_evidence and resolved.citation_ids == (1,)
