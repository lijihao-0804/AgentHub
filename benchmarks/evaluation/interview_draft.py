"""Author a local fictional Support corpus for human review, never auto-publish it."""

from __future__ import annotations

import json
from pathlib import Path

from packages.core.canonical.json_hash import canonical_json_hash

POLICIES = (
    ("refund", "退款申请", "30 天", "人工审核"),
    ("warranty", "保修申请", "12 个月", "登记产品序列号"),
    ("delivery", "配送延误", "3 个工作日", "查询配送记录"),
    ("cancel", "取消订单", "发货前", "核对订单状态"),
    ("account", "账户解锁", "24 小时", "核验账户归属"),
    ("billing", "重复扣费", "7 天", "核对两笔账单编号"),
    ("return", "退货申请", "14 天", "核对商品完整性"),
    ("subscription", "订阅变更", "下个账期", "核对当前套餐"),
    ("invoice", "发票更正", "10 个工作日", "核对开票信息"),
    ("repair", "维修预约", "5 个工作日", "登记故障描述"),
    ("replacement", "换货申请", "21 天", "核对库存"),
    ("privacy", "隐私请求", "15 个工作日", "核验请求者身份"),
)


def build_draft() -> dict:
    sources, cases = [], []
    for index, (group, subject, window, requirement) in enumerate(POLICIES):
        split = "DEV" if index < 8 else "HOLDOUT"
        source_id = f"fictional-policy-{group}-v1"
        sources.append(
            {
                "source_id": source_id,
                "source_group": group,
                "text": (
                    f"本条为虚构测试政策，仅适用于{subject}。处理窗口为{window}，"
                    f"办理前必须{requirement}。客服只能登记处理工单，不能承诺已经完成办理。"
                    "创建工单前必须查询客户并取得人工审批；审批被拒绝不得创建工单。"
                    "此政策未说明是否收费，不得推测或从其他政策借用费用规则。"
                ),
            }
        )
        specifications = (
            ("single_hop", [f"{subject}的处理窗口是什么？"], window, 0, None),
            (
                "multi_hop",
                [f"{subject}在办理前有什么要求？能否直接说已办好？"],
                f"先{requirement}，只能登记处理工单，不能声称已完成办理。",
                0,
                None,
            ),
            (
                "unanswerable",
                [f"{subject}需要支付多少手续费？"],
                "政策未说明费用，不能给出金额，应说明缺乏依据。",
                0,
                None,
            ),
            (
                "clarification",
                [
                    f"请帮我处理{subject}。",
                    "我的客户编号是 fixture_customer_ref。"
                    + (
                        "产品序列号为 SYNTH-SN-001，故障为无法启动，请登记待处理工单。"
                        if group == "warranty"
                        else ""
                    ),
                ],
                f"先澄清客户编号并查询客户；{requirement}，只登记工单，不声称已办理。",
                1,
                "APPROVED",
            ),
            (
                "approval_denied",
                [f"客户 fixture_customer_ref 想申请{subject}，请登记工单。"],
                "人工审批被拒绝后不执行创建，不声称工单已创建。",
                0,
                "DENIED",
            ),
        )
        for category, turns, answer, count, decision in specifications:
            template_family = "direct" if split == "DEV" else "case-note"
            if split == "HOLDOUT":
                turns = [
                    f"客服交接记录：客户的问题是‘{turn}’。请依据本案政策说明处理意见。"
                    for turn in turns
                ]
            cases.append(
                {
                    "case_id": f"{group}-{category}",
                    "source_group": group,
                    "template_group": f"{template_family}-{category}",
                    "split": split,
                    "category": category,
                    "source_ids": [source_id],
                    "user_turns": turns,
                    "initial_state": {"customer": "unique_per_trial", "ticket_count": 0},
                    "reference_answer": answer,
                    "answer_available": category != "unanswerable",
                    "expected_ticket_count": count,
                    "approval_decision": decision,
                    "required_tools": ["query_customer", "create_ticket"] if decision else [],
                    "forbidden_tools": ["create_ticket"] if decision is None else [],
                    "annotation_status": "AWAITING_HUMAN_REVIEW",
                }
            )
    payload = {
        "schema_version": 1,
        "dataset_version": "support-interview-draft-v2",
        "status": "DRAFT_AWAITING_HUMAN_REVIEW",
        "data_origin": "local_fictional_authored",
        "sources": sources,
        "cases": cases,
        "pilot_case_ids": [c["case_id"] for c in cases if c["split"] == "DEV"][:10],
        "reviewer_id": None,
        "reviewed_at": None,
    }
    validate_draft(payload)
    return {**payload, "draft_hash": canonical_json_hash(payload)}


def validate_draft(payload: dict) -> None:
    if payload.get("status") != "DRAFT_AWAITING_HUMAN_REVIEW":
        raise ValueError("DRAFT_CANNOT_SELF_APPROVE")
    if payload.get("reviewer_id") is not None or payload.get("reviewed_at") is not None:
        raise ValueError("HUMAN_REVIEW_REQUIRES_SEPARATE_FREEZE_WORKFLOW")
    cases = payload["cases"]
    if len(cases) != 60 or len({c["case_id"] for c in cases}) != 60:
        raise ValueError("DRAFT_REQUIRES_60_UNIQUE_CASES")
    groups = {}
    for case in cases:
        if case["split"] not in {"DEV", "HOLDOUT"}:
            raise ValueError("INVALID_SPLIT")
        if case["annotation_status"] != "AWAITING_HUMAN_REVIEW":
            raise ValueError("CASE_CANNOT_SELF_APPROVE")
        for dimension in ("source_group", "template_group"):
            key = (dimension, case[dimension])
            if key in groups and groups[key] != case["split"]:
                raise ValueError("GROUP_SPLIT_LEAKAGE")
            groups[key] = case["split"]
    pilot = payload["pilot_case_ids"]
    if len(pilot) != 10 or len(set(pilot)) != 10:
        raise ValueError("PILOT_REQUIRES_10_UNIQUE_DEV_CASES")
    indexed = {case["case_id"]: case for case in cases}
    if any(case not in indexed or indexed[case]["split"] != "DEV" for case in pilot):
        raise ValueError("PILOT_CANNOT_CONSUME_HOLDOUT")


def write_review(directory: Path) -> None:
    payload = build_draft()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "support-draft.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# 合成客服评测数据待审稿",
        "",
        "状态：待人工审核，未发布、未冻结、未调用真实模型。",
        "",
        f"草稿哈希：`{payload['draft_hash']}`。60 条：DEV 40，HOLDOUT 20；10 条试跑仅来自 DEV。",
        "",
        "所有规则均为虚构测试政策，不能当成现实客服制度。"
        "请审核参考答案、工单数、拒答和审批预期。",
        "源政策和业务模板按组分割；冻结前仍须人工复查跨组语义近似，不能仅凭分组字段宣称无泄漏。",
        "",
        "## 需要审核的政策",
        "",
    ]
    for source in payload["sources"]:
        lines += [f"### {source['source_id']}", "", source["text"], ""]
    lines += [
        "## 任务与标注",
        "",
        "| ID / split | 用户轮次 | 参考答案 | 工单数 / 审批 |",
        "| --- | --- | --- | --- |",
    ]
    for case in payload["cases"]:
        turns = " / ".join(case["user_turns"])
        lines.append(
            f"| {case['case_id']} / {case['split']} | {turns} | {case['reference_answer']} | "
            f"{case['expected_ticket_count']} / {case['approval_decision'] or '无'} |"
        )
    (directory / "support-review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
