"""Explicit, bounded live provider probe using AgentHub's existing adapter.

Reads only the named environment variables; never prints credentials or raw provider errors.
This is connectivity/capability smoke coverage, not a RAG quality experiment.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from packages.model_gateway.adapters.litellm.adapter import LiteLLMProviderAdapter
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelStreamEventType,
    ModelToolDefinition,
    StructuredOutputSchema,
)
from packages.model_gateway.errors import ModelGatewayError
from packages.model_gateway.models import ModelProfile, ProviderCredential

ROOT = Path(__file__).resolve().parents[2]


async def probe(config: dict, env_file: Path) -> dict:
    values = dotenv_values(env_file, encoding="utf-8-sig")
    secret = os.environ.get(config["api_key_env"]) or values.get(config["api_key_env"])
    base_url = os.environ.get(config["base_url_env"]) or values.get(config["base_url_env"])
    if not secret or not base_url:
        raise ValueError("Provider environment variables are missing")
    target = urlsplit(base_url)
    if target.scheme != "https" or not target.hostname or target.username or target.query:
        raise ValueError("Provider base URL must be an HTTPS API base without credentials/query")
    credential = ProviderCredential(provider=config["provider"], secret=secret, base_url=base_url)
    # These objects are not persisted. The production adapter only consumes request settings.
    profile = ModelProfile(
        model=config["model"],
        temperature=Decimal(config["temperature"]),
        max_tokens=min(config["max_tokens"], 512),
        timeout_seconds=Decimal(config["timeout_seconds"]),
    )
    adapter = LiteLLMProviderAdapter()
    results = []

    async def run(name: str, request: ModelRequest, *, stream: bool = False) -> bool:
        started = time.perf_counter()
        entry = {"test": name, "passed": False}
        try:
            if stream:
                deltas = 0
                response = None
                async for event in adapter.stream(profile, credential, request):
                    if event.event_type == ModelStreamEventType.MESSAGE_DELTA:
                        deltas += 1
                    if event.event_type == ModelStreamEventType.COMPLETED:
                        response = event.response
                entry["message_delta_events"] = deltas
                entry["passed"] = bool(response and response.content.strip() and deltas)
            else:
                response = await adapter.complete(profile, credential, request)
                if name == "tool_calling":
                    entry["passed"] = any(
                        call.name == "echo_probe" and call.arguments == {"value": "ok"}
                        for call in response.tool_calls
                    )
                elif name == "structured_output":
                    try:
                        entry["passed"] = json.loads(response.content) == {"status": "ok"}
                    except (ValueError, TypeError):
                        entry["failure_code"] = "OUTPUT_NOT_EXPECTED_JSON"
                else:
                    entry["passed"] = response.content.strip() == "OK"
            if response is not None:
                entry["finish_reason"] = response.finish_reason
                entry["usage"] = asdict(response.usage) if response.usage else None
                entry["response_characters"] = len(response.content)
        except ModelGatewayError as exc:
            entry["failure_code"] = str(exc.code)
        except Exception as exc:
            # Raw SDK exceptions may contain request headers or endpoint details.
            entry["failure_code"] = "PROBE_EXCEPTION"
            entry["error_type"] = type(exc).__name__
        entry["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        results.append(entry)
        return entry["passed"]

    basic = ModelRequest(messages=(ModelMessage(role="user", content="Reply exactly OK."),))
    if await run("chat_completion", basic):
        await run("streaming", basic, stream=True)
        await run(
            "tool_calling",
            ModelRequest(
                messages=(
                    ModelMessage(
                        role="user", content="Call echo_probe with value ok. Do not answer in text."
                    ),
                ),
                tools=(
                    ModelToolDefinition(
                        name="echo_probe",
                        description="Return the supplied text for a connectivity check.",
                        parameters={
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    ),
                ),
            ),
        )
        await run(
            "structured_output",
            ModelRequest(
                messages=(ModelMessage(role="user", content='Return {"status":"ok"}.'),),
                response_schema=StructuredOutputSchema(
                    name="connectivity_probe",
                    strict=True,
                    json_schema={
                        "type": "object",
                        "properties": {"status": {"type": "string", "enum": ["ok"]}},
                        "required": ["status"],
                        "additionalProperties": False,
                    },
                ),
            ),
        )
    return {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "model": config["model"],
        "provider": config["provider"],
        "request_max_tokens": profile.max_tokens,
        "automatic_retries": 0,
        "fallback_model": None,
        "database_profile_registered": False,
        "rag_experiments_run": False,
        "results": results,
        "note": (
            "Smoke checks only; strict schema enforcement, arbitrary tools, pricing, "
            "context limits and quality are not certified."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("model.json"))
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    logging.disable(logging.CRITICAL)
    # Provider libraries may print diagnostics; discard them instead of leaking secrets.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = asyncio.run(probe(config, args.env_file))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["results"] and result["results"][0]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
