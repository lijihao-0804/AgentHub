"""Export aggregate-only RAG evidence; original runs are never modified.

Run explicitly before a publication. This is a projection, not a replacement
for the full local evidence, and never labels a failed experiment successful.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path("benchmarks/public_rag/results")
OUTPUT = ROOT / "published"
OMIT = frozenset(
    {
        "cases",
        "rows",
        "answers",
        "records",
        "corpus",
        "annotations",
        "references",
        "answer_references",
        "gold_entities",
        "gold_evidence",
        "raw_calls",
        "outcomes",
        "messages",
        "request",
        "response",
        "raw_content",
        "content",
        "text",
        "snippet",
        "quote",
        "quotes",
        "paragraph_text",
        "chunk_text",
        "prepared",
        "source_map",
        "prompts",
        "prompt",
        "system_prompt",
        "api_key",
        "password",
        "secret",
        "authorization",
        "database_url",
        "connection_string",
        "secret_ciphertext",
    }
)
_LOCAL_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_CREDENTIAL_URL = re.compile(r"(?:postgres(?:ql)?|mysql|redis)\S*://[^\s/@]+:[^\s/@]+@", re.I)
_DROP = object()


def project(value, key=""):
    lower = key.casefold()
    if (
        lower in OMIT
        or lower.startswith("raw_")
        or any(token in lower for token in ("api_key", "password", "secret", "authorization"))
    ):
        return _DROP
    if isinstance(value, dict):
        result = {}
        for child_key, child in value.items():
            projected = project(child, str(child_key))
            if projected is not _DROP:
                result[child_key] = projected
        return result
    if isinstance(value, list):
        # Individual cases, source mappings and physical calls are not public aggregates.
        if any(isinstance(item, (dict, list)) for item in value) or len(value) > 100:
            return _DROP
        return [p for item in value if (p := project(item)) is not _DROP]
    if isinstance(value, str):
        if len(value) > 2000 or _CREDENTIAL_URL.search(value):
            return _DROP
        return "<local-path>" if _LOCAL_PATH.match(value) else value
    return value


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    manifest, groups = [], []
    for group in sorted(ROOT.iterdir()):
        if not group.is_dir() or group == OUTPUT:
            continue
        paths = sorted(p for p in group.rglob("*") if p.is_file())
        exported = []
        for path in paths:
            relative = path.relative_to(ROOT)
            row = {
                "local_artifact": path.as_posix(),
                "bytes": path.stat().st_size,
                "storage": "LOCAL_ONLY",
            }
            name = path.name
            aggregate = name.endswith(".json") and (
                "report" in name
                or "summary" in name
                or "acceptance" in name
                or name in {"decision.json", "protocol.json", "completed.json", "verification.json"}
            )
            if aggregate:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(raw, dict):
                    projected = project(raw)
                    projected["_publication"] = {
                        "kind": "AGGREGATE_ONLY_PROJECTION",
                        "source_artifact": path.as_posix(),
                        "not_full_raw_evidence": True,
                        "policy": (
                            "case text, source corpus, requests, credentials "
                            "and per-case objects omitted"
                        ),
                    }
                    serialized = json.dumps(projected, ensure_ascii=False, indent=2)
                    if len(serialized.encode("utf-8")) <= 512_000:
                        destination = OUTPUT / relative
                        dump(destination, projected)
                        exported.append(destination.relative_to(OUTPUT / group.name).as_posix())
                        row["aggregate_projection"] = destination.as_posix()
            manifest.append(row)
        target = OUTPUT / group.name / "README.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        links = "\n".join(f"- [{p}]({p})" for p in exported)
        target.write_text(
            f"# {group.name} — 公开汇总\n\n"
            "这里是完整本地实验记录的脱敏聚合投影，不包含逐题原文、模型请求或完整回放输入。"
            "原始成功、失败与未推广结论均保留；不要把汇总当作完整复现数据。\n\n"
            + (links or "本组没有可导出的聚合JSON；结论见研究报告。")
            + "\n",
            encoding="utf-8",
        )
        groups.append(
            {
                "name": group.name,
                "local_files": len(paths),
                "local_bytes": sum(p.stat().st_size for p in paths),
                "aggregate_files": len(exported),
            }
        )
    dump(
        OUTPUT / "local-artifact-manifest.json",
        {
            "policy": (
                "Original artifacts remain LOCAL_ONLY; "
                "aggregate projections do not replace them"
            ),
            "groups": groups,
            "artifacts": manifest,
        },
    )
    table = "\n".join(
        f"| [{g['name']}]({g['name']}/README.md) | {g['local_files']} | "
        f"{g['local_bytes'] / 1048576:.2f} | {g['aggregate_files']} |"
        for g in groups
    )
    OUTPUT.joinpath("README.md").write_text(
        "# RAG研究公开汇总与本地证据边界\n\n"
        "Git保存代码、研究报告和本目录的聚合投影。完整语料、逐题模型请求、数据库连接描述、"
        "运行JSONL和源码ZIP仍保留在本地原路径，不随Git上传。"
        "这意味着GitHub不是约466MB原始实验资料的完整备份。\n\n"
        "报告中指向 `benchmarks/public_rag/results/<实验>/...` 的链接是本地证据地址；"
        "在GitHub阅读时请使用下表汇总。`report-v2.json`等修复后的版本与早期错误报告分别保存，"
        "须按对应研究报告的说明解释，不可将错误报告当最终成绩。\n\n"
        "| 实验组 | 本地文件数 | 本地MiB | 公开聚合JSON |\n|---|---:|---:|---:|\n"
        + table
        + "\n\n[本地资料清单](local-artifact-manifest.json)只记路径与体积，不含原文或密钥。"
        "重新导出：`python -m scripts.export_rag_publication`；该命令不调用模型。\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "groups": len(groups),
                "local_artifacts": len(manifest),
                "aggregate_json": sum(g["aggregate_files"] for g in groups),
            }
        )
    )


if __name__ == "__main__":
    main()
