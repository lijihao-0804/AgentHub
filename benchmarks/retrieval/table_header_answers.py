"""One-shot paid WAI answer comparison. Requires the explicit --dispatch flag."""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path

import httpx

from benchmarks.retrieval.table_headers import PROTOCOL
from packages.core.canonical.json_parse import load_unique_json

MODEL = "qwen-plus-2025-07-28"
URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
SYSTEM = (
    "Answer the question using only the provided numbered table rows. Return JSON with "
    "value (exact cell text), source (1-based evidence number), and column (1-based position "
    "in that source row). If evidence is insufficient or ambiguous, return null for all three. "
    "Do not infer missing categories or use outside knowledge."
)
MAX_INPUT = 4096
MAX_OUTPUT = 96
RESERVE = Decimal("0.03")


def write_entry(file, data):
    file.write(json.dumps(data) + "\n")
    file.flush()
    os.fsync(file.fileno())


def run(args):
    api_key = os.environ["DASHSCOPE_API_KEY"]
    protocol = json.loads(PROTOCOL.read_text(encoding="utf8"))
    contexts = json.loads(args.contexts.read_text(encoding="utf8"))
    cases = protocol["cases"]
    if len(cases) != 14 or set(contexts) != {"baseline", "candidate"}:
        raise ValueError("Unexpected protocol.")
    # Even at the highest published long-context rates (4.8 in, 64 out / million)
    # each dispatch fits its reserve, without relying on cache or trial credits.
    assert (MAX_INPUT * Decimal("4.8") + MAX_OUTPUT * 64) / 10**6 < RESERVE
    assert RESERVE * len(cases) * 2 < Decimal("30")
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
    # Exclusive creation prevents replay of completed or uncertain requests in this experiment.
    with (
        args.ledger.open("x", encoding="utf8") as log,
        httpx.Client(timeout=90, follow_redirects=False) as client,
    ):
        for i, arm, rows, messages, gold, citation in requests:
            key = f"{arm}/{i + 1}"
            write_entry(log, {"key": key, "state": "reserved", "yuan": str(RESERVE)})
            response = client.post(
                URL,
                headers={"Authorization": "Bearer " + api_key},
                json={
                    "model": MODEL,
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
            usage = body.get("usage", {})
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if (
                body.get("model") != MODEL
                or type(prompt) is not int
                or type(completion) is not int
                or not 0 < prompt <= MAX_INPUT
                or not 0 <= completion <= MAX_OUTPUT
                or usage.get("total_tokens") != prompt + completion
            ):
                raise RuntimeError("Incomplete or inconsistent usage or model identity. Stop.")
            cost = (prompt * Decimal("0.8") + completion * 2) / 10**6
            write_entry(
                log,
                {
                    "key": key,
                    "state": "confirmed",
                    "id": body["id"],
                    "usage": usage,
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
                    "estimated_yuan": str(cost),
                }
            )
    args.output.write_text(
        json.dumps({"model": MODEL, "cases": summary}, indent=2) + "\n", encoding="utf8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("contexts", "ledger", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--dispatch", action="store_true", required=True)
    run(parser.parse_args())
