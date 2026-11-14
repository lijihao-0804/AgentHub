"""Render the observed first-response 502s from the archived public HTTP receipts."""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
GROUPS = (
    ("live", ("baseline", "candidate")),
    ("budget", ("512", "2048")),
    ("reader", ("A", "B", "C")),
    ("reader/authenticated-supplement", ("A", "B", "C")),
)


def render():
    lines = [
        "# 新实验中每次真实首答 502",
        "",
        "按首个 HTTP 结果逐条列出。输入仅为公开 API 回执；供应商输出阶段取自同一请求的适配器事件。"
        "401 在登录阶段另行报告。[此前实验的九次 502](history/README.md)不在本表分母。",
        "",
        "|批次和实际回执|题号|可观察到的故障|模型调用数|末次结束原因 / 输出 token|",
        "|---|---|---|---:|---|",
    ]
    for group, arms in GROUPS:
        for arm in arms:
            for path in sorted((HERE / group).glob(f"{arm}-*.json")):
                for row in json.loads(path.read_bytes())["rows"]:
                    if row["http"] != 502:
                        continue
                    events = row["events"]
                    calls = [e for e in events if e["kind"] == "wire"]
                    last = next((e for e in reversed(events) if e["kind"] == "response"), {})
                    if (
                        row["error"] == "MODEL_BAD_RESPONSE"
                        and last.get("content_present") is False
                    ):
                        failure = "网关：供应商回包无可见正文"
                    elif "JSON parsing failed" in row.get("message", ""):
                        failure = "严格 JSON 解析失败"
                    else:
                        failure = row["error"]
                    usage = last.get("usage") or {}
                    stop = last.get("finish_reason", "unknown")
                    detail = f"{stop} / {usage.get('output_tokens', 'unknown')}"
                    link = path.relative_to(HERE).as_posix()
                    lines.append(
                        f"|[{group} / {path.stem}]({link})|{row['id']}|{failure}|"
                        f"{len(calls)}|{detail}|"
                    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(render(), end="")
