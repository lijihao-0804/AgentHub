"""Live judge probe for AE-1..AE-4 on the Alibaba compatible-mode endpoint.

This is a bounded, manual experiment: three short answers, one or three judge calls each,
recorded only as a JSON artifact.  It does not create a workspace, agent or evaluation run
in the database, so it cannot alter any persisted experiment.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.evaluation.judge import (
    AnswerQualityJudge,
    FrozenJudgeProfile,
    JudgeEnsemble,
    JudgeRequest,
)


class HttpJudgeClient:
    def __init__(self, *, base_url: str, api_key: str, model: str, max_tokens: int = 1500) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.usage: list[dict[str, Any]] = []

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": self.max_tokens,
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            payload = response.json()
        self.usage.append(payload.get("usage") or {})
        message = payload["choices"][0]["message"]
        # Reasoning models may spend the budget in reasoning_content; the judge contract
        # needs the final JSON, so never feed chain-of-thought into the parser.
        return message.get("content") or ""


def _profile(model: str, temperature: float, index: int) -> FrozenJudgeProfile:
    return FrozenJudgeProfile(
        provider="alibaba-maas",
        model=model,
        temperature=temperature,
        max_tokens=1500,
        timeout_seconds=90,
        profile_id=f"00000000-0000-0000-0000-{index:012d}",
        credential_ref=f"00000000-0000-0000-0000-{index + 100:012d}",
    )


CASES = [
    {
        "case_id": "good",
        "question": "客户要求退一台已激活耳机，订单 A-1001，购买 5 天。政策：7 天内可退，激活不影响。",  # noqa: E501
        "reference": "可退；核实订单后创建退货单，3 个工作日内退款。",
        "answer": "已核实订单 A-1001：购买 5 天，属于 7 天内无理由退货范围，激活不影响退货。已为客户创建退货单，预计 3 个工作日内退款。",  # noqa: E501
    },
    {
        "case_id": "partial",
        "question": "客户要求退一台已激活耳机，订单 A-1001，购买 5 天。政策：7 天内可退，激活不影响。",  # noqa: E501
        "reference": "可退；核实订单后创建退货单，3 个工作日内退款。",
        "answer": "可以退的，你提交一下订单号就行。",
    },
    {
        "case_id": "wrong",
        "question": "客户要求退一台已激活耳机，订单 A-1001，购买 5 天。政策：7 天内可退，激活不影响。",  # noqa: E501
        "reference": "可退；核实订单后创建退货单，3 个工作日内退款。",
        "answer": "已激活的耳机一律不能退，这是公司规定。",
    },
]


async def main() -> int:
    base_url = os.environ["AE_BASE_URL"]
    api_key = os.environ["AE_API_KEY"]
    model = os.environ.get("AE_MODEL", "deepseek-v4.1-flash")
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "ae-live-judge.json")

    members = [
        AnswerQualityJudge(
            _profile(model, temperature, index),
            HttpJudgeClient(base_url=base_url, api_key=api_key, model=model),
        )
        for index, temperature in enumerate((0.0, 0.2, 0.4), start=1)
    ]
    ensemble = JudgeEnsemble(members, strategy="MAJORITY")

    results = []
    for case in CASES:
        request = JudgeRequest(
            question=case["question"],
            answer=case["answer"],
            reference_answer=case["reference"],
        )
        single = await members[0].judge(request)
        combined = await ensemble.judge(request)
        results.append(
            {
                "case_id": case["case_id"],
                "single": {
                    "status": single.get("status"),
                    "score": single.get("score"),
                    "dimension_scores": single.get("dimension_scores"),
                    "reason": single.get("reason"),
                },
                "ensemble": {
                    "status": combined.get("status"),
                    "score": combined.get("score"),
                    "dimension_scores": combined.get("dimension_scores"),
                    "agreement": combined.get("judge_agreement"),
                    "reason": combined.get("reason"),
                },
            }
        )

    usage = [item for member in members for item in getattr(member.client, "usage", [])]
    artifact = {
        "model": model,
        "endpoint": base_url,
        "case_count": len(CASES),
        "judge_calls": len(usage),
        "prompt_tokens": sum(int(item.get("prompt_tokens") or 0) for item in usage),
        "completion_tokens": sum(int(item.get("completion_tokens") or 0) for item in usage),
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in usage),
        "results": results,
        "note": "Manual probe only; not a persisted experiment and not a model-quality claim.",
    }
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(artifact, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
