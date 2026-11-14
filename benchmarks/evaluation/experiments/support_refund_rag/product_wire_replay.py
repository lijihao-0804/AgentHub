"""Replay captured, admitted product requests through the physical meter's input policy."""

import argparse
from pathlib import Path
from types import SimpleNamespace

from benchmarks.public_rag.confirmation_meter import INPUT, input_bound
from packages.model_gateway.adapters.litellm.adapter import _request_payload
from packages.model_gateway.contracts import (
    AliyunDeepSeekRequestMode,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ModelToolDefinition,
)

from .runtime_trial import MODEL, read, save


def wire_bounds(calls):
    profile = SimpleNamespace(model=MODEL, temperature=0, max_tokens=2048, timeout_seconds=60)
    credential = SimpleNamespace(
        provider="openai-compatible",
        secret="",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    bounds = []
    for call in calls:
        raw = call["request"]
        msgs = [
            ModelMessage(
                **(msg | {"tool_calls": tuple(ModelToolCall(**t) for t in msg["tool_calls"])})
            )
            for msg in raw["messages"]
        ]
        if raw["response_schema"] is not None:
            raise ValueError("unexpected structured output for tool Agent")
        req = ModelRequest(
            messages=tuple(msgs),
            tools=tuple(ModelToolDefinition(**t) for t in raw["tools"]),
            request_mode=AliyunDeepSeekRequestMode(raw["request_mode"]),
        )
        payload = _request_payload(profile, credential, req)
        fields = ("messages", "tools", "response_format", "max_tokens", "temperature", "extra_body")
        bounds.append(input_bound({k: payload[k] for k in fields if k in payload}))
    return bounds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = {}
    for case in read(args.capture / "report.json")["rows"]:
        key = case["case_id"]
        rows[key] = wire_bounds(read(args.capture / (key + ".json"))["calls"])
    bounds = [n for v in rows.values() for n in v]
    save(
        args.output,
        {
            "scope": "contract provider request replay; no model dispatch",
            "rows": rows,
            "calls": len(bounds),
            "max": max(bounds, default=0),
            "limit": INPUT,
            "over_limit": sum(n > INPUT for n in bounds),
        },
    )
    return int(any(n > INPUT for n in bounds))


if __name__ == "__main__":
    raise SystemExit(main())
