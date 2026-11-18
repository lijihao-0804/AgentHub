"""One-shot paid WAI answer comparison. Requires the explicit --dispatch flag."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from packages.core.canonical.json_parse import load_unique_json
from scripts.aliyun_api_smoke import aliyun_route

PROTOCOL = Path(__file__).with_name("table_header_protocol.json")
CONTEXTS_SHA256 = "41a257164089a3fd958186ba6a06ae9aa84e4cd098f6ca967aee428e1764d6c3"
SYSTEM = (
    "Answer the question using only the provided numbered table rows. Return JSON with "
    "value (exact cell text), source (1-based evidence number), and column (1-based position "
    "in that source row). If evidence is insufficient or ambiguous, return null for all three. "
    "Do not infer missing categories or use outside knowledge."
)
MAX_INPUT = 4096
MAX_OUTPUT = 96
RESERVE = Decimal("0.009")


def receipt_cost(body, model, *, max_input=MAX_INPUT, max_output=MAX_OUTPUT):
    """Calculate Beijing DeepSeek Flash tariff from reported usage, including cache hits."""
    usage = body.get("usage") or {}
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens", usage.get("prompt_cache_hit_tokens"))
    if (
        body.get("model") != model
        or not body.get("id")
        or type(body.get("created")) is not int
        or type(prompt) is not int
        or type(completion) is not int
        or type(cached) is not int
        or not 0 <= cached <= prompt <= max_input
        or prompt == 0
        or not 0 <= completion <= max_output
        or usage.get("total_tokens") != prompt + completion
    ):
        raise ValueError("Incomplete usage, tariff timestamp or model identity.")
    hour = datetime.fromtimestamp(body["created"], timezone(timedelta(hours=8))).hour
    multiplier = 2 if 8 <= hour < 22 else 1
    yuan = (prompt - cached + cached * Decimal("0.1") + completion * 4) * multiplier / 10**6
    return prompt, completion, cached, yuan


def write_entry(file, data):
    file.write(json.dumps(data) + "\n")
    file.flush()
    os.fsync(file.fileno())


def run(args):
    route = aliyun_route()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf8"))
    contexts = json.loads(args.contexts.read_text(encoding="utf8"))
    cases = protocol["cases"]
    evidence_hash = hashlib.sha256(
        json.dumps(contexts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if len(cases) != 14 or evidence_hash != CONTEXTS_SHA256:
        raise ValueError("Unexpected protocol.")
    if (MAX_INPUT * 2 + MAX_OUTPUT * 8) / Decimal(10**6) > RESERVE:
        raise ValueError("Peak Beijing tariff exceeds the dispatch reservation.")
    requests = []
    for i, (question, _, _, gold, citation) in enumerate(cases):
        arms = ("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline")
        for arm in arms:
            rows = contexts[arm][i]
            user = (
                question + "\n" + "\n".join(f"{j}. {row['text']}" for j, row in enumerate(rows, 1))
            )
            messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
            # UTF-8 bytes upper bound the byte-based tokenizer input; leave room for framing.
            if len(SYSTEM.encode()) + len(user.encode()) + 128 > MAX_INPUT:
                raise ValueError("Frozen request input bound exceeded.")
            requests.append((i, arm, rows, messages, gold, citation))
    summary = []
    receipt_ids = set()
    # Exclusive creation prevents replay of completed or uncertain requests in this experiment.
    with (
        args.ledger.open("x", encoding="utf8") as log,
        httpx.Client(timeout=float(route.timeout_seconds), trust_env=False) as client,
    ):
        for i, arm, rows, messages, gold, citation in requests:
            key = f"{arm}/{i + 1}"
            write_entry(log, {"key": key, "state": "reserved", "yuan": str(RESERVE)})
            response = client.post(
                route.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + route.api_key},
                json={
                    "model": route.model,
                    "messages": messages,
                    "temperature": 0,
                    "enable_thinking": False,
                    "max_tokens": MAX_OUTPUT,
                    "response_format": {"type": "json_object"},
                },
            )
            if not response.is_success:
                write_entry(log, {"key": key, "state": "unconfirmed", "http": response.status_code})
                raise RuntimeError("Provider did not return a successful receipt. Stop.")
            body = response.json()
            try:
                prompt, completion, cached, cost = receipt_cost(body, route.model)
                if body["id"] in receipt_ids:
                    raise ValueError("Duplicate provider receipt.")
            except ValueError:
                write_entry(log, {"key": key, "state": "unconfirmed", "http": response.status_code})
                raise
            receipt_ids.add(body["id"])
            write_entry(
                log,
                {
                    "key": key,
                    "state": "confirmed",
                    "id": body["id"],
                    "usage": body["usage"],
                    "created": body["created"],
                    "yuan": str(cost),
                },
            )
            source = column = value = position = None
            valid = False
            try:
                payload = load_unique_json(body["choices"][0]["message"]["content"])
                value, source, column = (payload[k] for k in ("value", "source", "column"))
                if set(payload) == {"value", "source", "column"}:
                    if value is source is column is None:
                        valid = True
                    elif (
                        isinstance(value, str)
                        and type(source) is type(column) is int
                        and 1 <= source <= len(rows)
                    ):
                        selected = rows[source - 1]
                        if 1 <= column <= len(selected["text"].split(" | ")):
                            position = [selected["page"] - 1, selected["row"], column]
                            valid = True
            except (TypeError, KeyError, ValueError, IndexError):
                pass
            summary.append(
                {
                    "arm": arm,
                    "case": i + 1,
                    "positive": gold is not None,
                    "valid": valid,
                    "value_correct": valid and value == gold,
                    "citation_correct": valid and position == citation,
                    "joint_correct": valid and value == gold and position == citation,
                    "refused": valid and value is None,
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "cached_input_tokens": cached,
                    "estimated_yuan": str(cost),
                }
            )
    args.output.write_text(
        json.dumps(
            {"model": route.model, "contexts_sha256": evidence_hash, "cases": summary}, indent=2
        )
        + "\n",
        encoding="utf8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("contexts", "ledger", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--dispatch", action="store_true", required=True)
    run(parser.parse_args())
