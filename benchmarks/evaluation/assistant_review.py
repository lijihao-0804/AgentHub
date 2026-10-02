"""Publish explicitly assistant-authored review decisions, never human agreement."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from packages.core.canonical.json_hash import canonical_json_hash

REVIEW_VERSION = "assistant-support-review-v1"
REVIEWED_REPORT_HASHES = {
    "live-pilot.json": "f51f629118a877182f8c480b6751e5e02a598901b4eb009232fdc11b995f38da",
    "live-pilot-candidate.json": "9bd0d7d79d9d279451c8adb6899f7f8deb558a24dd703fc74cb852969b7b899b",
    "live-pilot-v2-three-trials-recovery.json": (
        "e7b552bec9a4b9969d879aae7e58e1c6b1a63925d75be86da1348606d7e599e5"
    ),
    "live-pilot-state-clarity.json": (
        "cb2bb9fb5466dfe92bd3e21ce8695e019c07595083474213afeb72ebb489a967"
    ),
    "live-dev-registration.json": (
        "b9f8c7af0655a1d3730241995c644f7cdc5f68470f9840edff5e242d758e794c"
    ),
    "live-dev-registration-remaining.json": (
        "dfffc9840e4c6bfd92114406d4e98eb87a2494cd96a7f91ce6e1e446853f7b0b"
    ),
}


def review(directory: Path):
    entries = []
    for name in REVIEWED_REPORT_HASHES:
        report = json.loads((directory / name).read_text(encoding="utf-8"))
        if canonical_json_hash(report) != REVIEWED_REPORT_HASHES[name]:
            raise ValueError("UNREVIEWED_REPORT_CONTENT_CHANGED")
        for case in report["cases"]:
            case_id = case["case_id"]
            repetition = case.get("repetition", 0)
            verdict, reason = "PASS", "回答符合虚构政策和参考目标，未声称业务办理已完成。"
            if case_id.endswith("unanswerable"):
                reason = "明确说明政策没有费用信息，未编造金额。"
            if name == "live-pilot.json":
                if case_id == "refund-clarification":
                    verdict, reason = "FAIL", "查询后要求额外材料，没有提交用户要求的工单创建请求。"
                elif case_id.endswith("approval_denied"):
                    verdict, reason = "NOT_EVALUABLE", "未进入审批拒绝分支，不能验证拒绝后的答复。"
                elif case_id == "warranty-clarification":
                    verdict, reason = (
                        "NOT_EVALUABLE",
                        "v1 缺必需序列号；继续澄清合理，工单数标签需要复核。",
                    )
            elif name == "live-pilot-candidate.json":
                if case_id == "refund-clarification":
                    verdict, reason = (
                        "FAIL",
                        "数据库已批准并创建；答复把 OPEN 说成待审批，未区分创建审批与业务核验。",
                    )
                elif case_id == "warranty-clarification":
                    verdict, reason = "NOT_EVALUABLE", "v1 缺必需序列号；不能沿用 v2 的成功结论。"
            elif name == "live-pilot-v2-three-trials-recovery.json":
                if case_id == "refund-clarification" or (
                    case_id == "warranty-clarification" and repetition in {1, 2}
                ):
                    verdict, reason = (
                        "FAIL",
                        "创建审批已通过且有工单；答复仍写待审批，业务核验与创建审批边界含糊。",
                    )
            elif (
                name == "live-pilot-state-clarity.json"
                and case_id == "refund-clarification"
                and repetition == 1
            ):
                verdict, reason = (
                    "FAIL",
                    "查询客户后再次询问是否登记，未沿用既有请求；无工单与审批。",
                )
            elif name.startswith("live-dev-registration"):
                if case_id == "delivery-single_hop":
                    verdict, reason = (
                        "FAIL",
                        "将查询配送记录升级成登记工单前置；政策仅要求业务办理前核验。",
                    )
                elif case_id == "delivery-clarification" and repetition == 1:
                    verdict, reason = (
                        "FAIL",
                        "上一轮已说明配送延误，答复仍称未说明诉求类型；与对话记录冲突。",
                    )
            entries.append(
                {
                    "source_file": name,
                    "source_report_hash": canonical_json_hash(report),
                    "dataset_hash": report["dataset_hash"],
                    "case_id": case_id,
                    "repetition": repetition,
                    "output_hash": canonical_json_hash(case["outputs"]),
                    "reviewer_kind": "assistant",
                    "reviewer": "Codex",
                    "review_version": REVIEW_VERSION,
                    "verdict": verdict,
                    "reason": reason,
                }
            )
    summaries = {}
    for name in {entry["source_file"] for entry in entries}:
        selected = [e for e in entries if e["source_file"] == name]
        counts = Counter(e["verdict"] for e in selected)
        summaries[name] = {
            "planned": len(selected),
            **{
                k: counts[k]
                for k in (
                    "PASS",
                    "FAIL",
                    "NOT_EVALUABLE",
                )
            },
        }
    result = {
        "review_version": REVIEW_VERSION,
        "reviewer_kind": "assistant",
        "user_authorization": "2026-10-02 user delegated review and requested autonomous execution",
        "independent_human_labels_available": False,
        "Q08": {"status": "NOT_AVAILABLE", "reason": "no_independent_human_labels"},
        "rubric": [
            "事实符合本项目虚构政策；不声称已完成退款/保修等业务办理",
            "用户请求登记时核验工具/审批/工单证据；不能只看运行 SUCCEEDED",
            "明确区分工单创建审批、OPEN 待处理与后续业务核验",
            "未进入拒绝分支或输入与标签冲突时记 NOT_EVALUABLE",
        ],
        "summaries": summaries,
        "entries": entries,
        "v2_combined_business_success": {
            "definition": "operational postconditions AND assistant answer review PASS",
            "passed_trials": 25,
            "planned_trials": 30,
            "all_three_passed_tasks": 8,
            "planned_tasks": 10,
            "scope": "DEV pilot; assistant review; not independent human quality benchmark",
        },
        "v2_refusal": {
            "correct_refusals": 6,
            "unanswerable_trials": 6,
            "false_refusals": 0,
            "answerable_information_trials": 12,
            "excluded": "clarification and approval denial are not information refusal tasks",
        },
        "state_clarity_combined_business_success": {
            "passed_trials": 29,
            "planned_trials": 30,
            "all_three_passed_tasks": 9,
            "planned_tasks": 10,
            "scope": "same DEV v2 dataset; only prompt changed; assistant review",
        },
        "dev_registration_combined_business_success": {
            "passed_trials": 116,
            "planned_trials": 120,
            "all_three_passed_tasks": 38,
            "planned_tasks": 40,
            "operational_postconditions_passed": 120,
            "scope": (
                "40 DEV cases x3; primary facts, state and conversation consistency; "
                "assistant review"
            ),
        },
    }
    (directory / "assistant-review.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rows = [
        "# 助手复核记录",
        "",
        "审阅者：Codex；类型：assistant；版本：" + REVIEW_VERSION,
        "",
        "用户已委托助手复核。原真人表保持空白；Q08 真人一致性未知。",
        "200 条记录包含重复任务和版本，覆盖 40 个 DEV 任务，不是 200 个独立任务。",
        "",
        "| 来源 | 任务/重复 | 判定 | 理由 |",
        "| --- | --- | --- | --- |",
    ]
    rows += [
        f"| {e['source_file']} | {e['case_id']}/{e['repetition']} | "
        f"{e['verdict']} | {e['reason']} |"
        for e in entries
    ]
    rows += [
        "",
        "v2：操作状态 30/30；结合助手答案审阅 25/30，三次全通过 8/10。",
        "状态澄清版：结合助手答案审阅 29/30，三次全通过 9/10；漏登记 1 条保留为失败。",
        "扩大 DEV：状态 120/120；结合助手审阅 116/120，三次全通过 38/40。",
        "保留 4 条失败：配送问题 3 条错误前置条件、1 条对话事实冲突。",
        "未使用助手标签冒充真人 judge agreement；未将历史状态指标改写为质量指标。",
        "",
    ]
    (directory / "assistant-review.md").write_text("\n".join(rows), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    review(parser.parse_args().directory)
