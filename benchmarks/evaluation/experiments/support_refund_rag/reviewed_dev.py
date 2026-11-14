"""Frozen DEV scoring amendments. Only the assessor may import these labels."""

from copy import deepcopy

from .runtime_trial import ROOT, read


def reviewed_cases():
    cases = deepcopy(read(ROOT / "data/cases.json")["questions"])
    review = read(ROOT / "posthoc-equivalence-review.json")["cases"]
    by_id = {case["case_id"]: case for case in cases}
    for row in review:
        key = row["case_id"]
        case = by_id[key]
        original = case["evidence_groups"][0]["all_of"]
        if key not in {"S0-005", "S0-013", "S0-032", "S0-036", "S0-038"}:
            continue
        proof = row["visible_proofs"][0]
        atoms = [{"source_id": proof["source"], "quote": proof["actual_visible_quote"]}]
        if key == "S0-032":
            atoms.extend(
                [
                    original[2],
                    {"source_id": proof["source"], "quote": "两个版本都从实际签收日次日起算"},
                ]
            )
            case["key_facts"].append("新版为 60 个自然日")
        if key == "S0-036":
            atoms.append(
                {
                    "source_id": proof["source"],
                    "quote": "这是 2025 年历史版本。2026-01-01 起由 POL-REFUND-WINDOW-01 替代",
                }
            )
        if key == "S0-038":
            atoms.append(original[1])
        case["evidence_groups"].append({"label": "source_reviewed_equivalent", "all_of": atoms})
    case = by_id["S0-003"]
    case["key_facts"].extend(
        ["安装验收单或客户签收单均可启动窗口", "本次订单查询未返回退款记录，历史申请情况未知"]
    )
    atom = {
        "source_id": "POL-CHANNEL-MARKETPLACE-LONG-01",
        "quote": "只有安装验收单或客户签收单可以启动本政策窗口",
    }
    for group in case["evidence_groups"]:
        if atom not in group["all_of"]:
            group["all_of"].append(atom)
    return cases
