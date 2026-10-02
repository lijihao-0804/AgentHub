"""Assistant-reviewed source data; formal publication uses the dataset service later."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path

from benchmarks.evaluation.interview_draft import POLICIES, build_draft
from packages.core.canonical.json_hash import canonical_json_hash
from packages.evaluation.validation import validate_dataset_items


def formal_items(data=None):
    """Normalize labels separately from scenario inputs; publication remains explicit."""
    data = data or reviewed_source()
    validate_reviewed_source(data)
    return validate_dataset_items(
        [
            {
                "case_key": case["case_id"],
                "split": case["split"],
                "category": "MULTI_STEP",
                "input": {
                    "task": case["user_turns"][0],
                    "scenario": {
                        "schema_version": 1,
                        "fixture_kind": "support_customer",
                        "user_turns": case["user_turns"],
                    },
                },
                "expected": {
                    "steps": case["required_tools"],
                    "terminal_status": "SUCCEEDED",
                    "scenario": {
                        "schema_version": 1,
                        "ticket_count": case["expected_ticket_count"],
                        "approval_decisions": [case["approval_decision"]]
                        if case["approval_decision"]
                        else [],
                        "required_tools": case["required_tools"],
                        "forbidden_tools": case["forbidden_tools"],
                        "reference_answer": case["reference_answer"],
                        "answer_available": case["answer_available"],
                    },
                },
                "tags": ["synthetic", case["category"], "assistant-reviewed"],
                "source_provenance": {
                    "source_kind": "assistant_reviewed_synthetic",
                    "source_id": case["case_id"],
                    "source_group": case["source_group"],
                    "template_group": case["template_group"],
                    "source_content_hash": data["content_hash"],
                    "reviewer_kind": "assistant",
                },
                "ordinal": ordinal,
            }
            for ordinal, case in enumerate(data["cases"])
        ]
    )


def reviewed_source():
    original = build_draft()
    data = deepcopy(original)
    data.pop("draft_hash")
    data["dataset_version"] = "support-assistant-reviewed-v3"
    data["status"] = "ASSISTANT_REVIEWED_SOURCE"
    data["reviewer_kind"] = "assistant"
    data["reviewer_id"] = "Codex"
    data["reviewed_at"] = "2026-10-02"
    data["source_draft_hash"] = original["draft_hash"]
    data["authorization"] = "user delegated review and authorized direct synthetic data use"
    data["published_dataset_version_id"] = None
    data["independent_human_labels_available"] = False
    policies = {p[0]: p for p in POLICIES}
    for case in data["cases"]:
        _, subject, window, requirement = policies[case["source_group"]]
        category = case["category"]
        if case["split"] == "HOLDOUT":
            templates = {
                "single_hop": (
                    "policy-error-correction",
                    [f"新人把{subject}的处理窗口写成‘政策未规定’。请核对并纠正这一条记录。"],
                ),
                "multi_hop": (
                    "completion-claim-audit",
                    [
                        f"交接单写着‘{subject}已办结，没有其他前置要求’。"
                        "请给主管写一段纠正通知，列出政策要求与客服实际权限。"
                    ],
                ),
                "unanswerable": (
                    "fee-proposal-verification",
                    [
                        f"财务拟对{subject}每笔收取 20 元手续费。"
                        "请核验这个金额能否由本案政策推导，并解释依据是否充足。"
                    ],
                ),
                "clarification": (
                    "handover-missing-identity",
                    [
                        f"交接待办：客户希望登记{subject}的待处理工单，客户编号暂缺。请先指出缺项。",
                        "已补档：客户编号为 fixture_customer_ref。"
                        "请查询客户并登记待处理工单，材料待人工核验，不承诺业务办结。",
                    ],
                ),
                "approval_denied": (
                    "denial-stop-condition",
                    [
                        f"为客户 fixture_customer_ref 登记{subject}的待处理工单。"
                        "如创建审批拒绝，请结束本次请求，明确是否存在工单，不再重复申请。"
                    ],
                ),
            }
            family, turns = templates[category]
            case["user_turns"] = turns
            case["template_group"] = family
        case["annotation_status"] = "ASSISTANT_REVIEWED"
        case["annotation_review"] = {
            "reviewer_kind": "assistant",
            "reviewer_id": "Codex",
            "policy_window": window,
            "business_prerequisite": requirement,
            "accepted_for_development": case["split"] == "DEV",
            "rationale": {
                "single_hop": "窗口与源政策一致；不允许借用其他政策。",
                "multi_hop": "核验前置要求和客服权限；实际为单文档多事实，不称跨文档推理。",
                "unanswerable": "政策没有费用依据；未知费用不能推导成免费或 20 元。",
                "clarification": "先补客户编号并查询；创建须经审批，登记不等于业务办结。",
                "approval_denied": "以实际拒绝记录和零工单验收，未触发审批不能算该分支通过。",
            }[category],
        }
    data["limitations"] = [
        "synthetic policies and correlated category patterns, not real production distribution",
        "multi_hop label retained for source compatibility; only multi-fact in one policy",
        (
            "source/template grouping and assistant semantic review "
            "do not prove absence of all leakage"
        ),
        (
            "published_dataset_version_id remains null until normalizing "
            "through strict scenario contract"
        ),
    ]
    validate_reviewed_source(data)
    return {**data, "content_hash": canonical_json_hash(data)}


def validate_reviewed_source(data):
    cases = data["cases"]
    sources = {s["source_id"]: s for s in data["sources"]}
    if len(cases) != 60 or len({c["case_id"] for c in cases}) != 60:
        raise ValueError("REVIEWED_SOURCE_REQUIRES_60_CASES")
    if data["reviewer_kind"] != "assistant" or data["independent_human_labels_available"]:
        raise ValueError("CANNOT_CLAIM_HUMAN_REVIEW")
    groups = {}
    for case in cases:
        if case["split"] not in {"DEV", "HOLDOUT"}:
            raise ValueError("INVALID_SPLIT")
        if not case["source_ids"] or any(s not in sources for s in case["source_ids"]):
            raise ValueError("UNKNOWN_POLICY")
        for dimension in ("source_group", "template_group"):
            key = (dimension, case[dimension])
            if key in groups and groups[key] != case["split"]:
                raise ValueError("GROUP_SPLIT_LEAKAGE")
            groups[key] = case["split"]
        if not case["user_turns"] or not all(
            isinstance(t, str) and t.strip() for t in case["user_turns"]
        ):
            raise ValueError("INVALID_TURNS")
    if sum(c["split"] == "DEV" for c in cases) != 40:
        raise ValueError("INVALID_SPLIT_COUNT")


def write(directory: Path):
    directory.mkdir(parents=True, exist_ok=False)
    data = reviewed_source()
    (directory / "support-reviewed.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rows = [
        "# 60 条合成客服数据：助手标注复核",
        "",
        "审阅者 Codex，类型 assistant。用户已委托审阅，未冒充真人标注。",
        "状态：源数据已复核；未发布数据库 DatasetVersion，未消费 HOLDOUT。",
        "DEV 输入/标签保留 v2；HOLDOUT 改为纠错、交接、费用提案核验等独立措辞，",
        "不再只给 DEV 问题包一层‘交接记录’。共同业务约束仍相关，不能声称零语义泄漏。",
        "所谓 multi_hop 当前是同一政策的多事实任务，尚无跨文档推理证据。",
        "",
        f"内容哈希：`{data['content_hash']}`",
        "",
        "| 任务 | split / 模板 | 审阅结论 |",
        "| --- | --- | --- |",
    ]
    rows += [
        f"| {c['case_id']} | {c['split']} / {c['template_group']} | "
        f"{c['annotation_review']['rationale']} |"
        for c in data["cases"]
    ]
    (directory / "support-reviewed.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    write(parser.parse_args().directory)
