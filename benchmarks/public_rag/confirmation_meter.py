"""Single-dispatch billing guard for this frozen public QA and refund study only."""

import argparse
import json
import sqlite3
from contextlib import closing
from functools import cache
from pathlib import Path

from packages.agent_runtime.deepseek_v41_counter import TOKENIZER_REVISION, DeepseekV41TextCounter
from packages.model_gateway.adapters.litellm.adapter import (
    _is_verified_aliyun_beijing_openai_endpoint,
)
from packages.model_gateway.contracts import ModelMessage, ModelRequest

MODEL = "deepseek-v4.1-flash-1017"
INPUT, OUTPUT = 32768, 2048
# Costs in millionths of a yuan, using Beijing peak prices per million tokens.
INPUT_PRICE, OUTPUT_PRICE = 2, 8
LIMITS = {"answer": (120, 1), "selector": (40, 1), "refund_dev": (200, 4), "refund_new": (8, 4)}
PER_REQUEST = INPUT * INPUT_PRICE + OUTPUT * OUTPUT_PRICE
TOOL_TEMPLATE_ALLOWANCE = 2048


@cache
def text_counter():
    asset = (
        Path(__file__).resolve().parents[2] / ".scratch/model-assets" / MODEL / TOKENIZER_REVISION
    )
    return DeepseekV41TextCounter.from_file(asset / "tokenizer.json")


def input_bound(payload):
    """Estimate input tokens for admission, never for settlement.

    Plain non-thinking text uses the pinned DeepSeek chat template. Other inputs
    encode the entire serialized provider payload plus 2,048 template tokens.
    Provider tool templating is private, so this margin is not a proven upper bound.
    Each reservation still uses INPUT and OUTPUT at peak prices. Only reported
    usage settles it; missing or over-limit usage leaves the study locked.
    """
    messages = payload["messages"]
    if (
        payload.get("extra_body") == {"enable_thinking": False}
        and not payload.get("tools")
        and not payload.get("response_format")
        and all(set(m) == {"role", "content"} for m in messages)
    ):
        request = ModelRequest(messages=tuple(ModelMessage(**m) for m in messages))
        return text_counter().count(request, thinking=False) + 256
    wire_text = json.dumps({"model": MODEL, **payload}, ensure_ascii=False, allow_nan=False)
    return text_counter().count_serialized_text(wire_text) + TOOL_TEMPLATE_ALLOWANCE


def init_ledger(path: Path, *, approval_id: str, cap_microcny: int):
    if (
        not approval_id.strip()
        or not 0 < cap_microcny <= sum(n for n, _ in LIMITS.values()) * PER_REQUEST
    ):
        raise ValueError("An explicit fresh authorization and bounded CNY cap are required")
    # Exclusive creation prevents resetting the counter if a process restarts.
    path.touch(exist_ok=False)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE authorization (reference TEXT NOT NULL, cap INTEGER NOT NULL);
            CREATE TABLE wires (id INTEGER PRIMARY KEY, stage TEXT, task TEXT,
              status TEXT, input INTEGER, output INTEGER, cost INTEGER, provider_id TEXT,
              failure TEXT, http_status INTEGER, local_input_estimate INTEGER);
        """)
        conn.execute("INSERT INTO authorization VALUES (?, ?)", (approval_id, cap_microcny))


class AliyunMeter:
    """Use the already egress-guarded SDK client, with zero redirects/retries."""

    def __init__(self, path: Path, stage: str):
        if stage not in LIMITS or not path.is_file():
            raise ValueError("Unknown stage or missing study authorization")
        self.path, self.stage, self.task_id = path, stage, ""

    def reserve(self, local_input_estimate=None):
        if not self.task_id:
            raise ValueError("Missing scheduled task identity")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM wires WHERE status != 'returned' LIMIT 1").fetchone():
                raise RuntimeError("Unsettled physical request; study stopped")
            cap = db.execute("SELECT cap FROM authorization").fetchone()[0]
            all_cost = db.execute("SELECT COALESCE(sum(cost),0) FROM wires").fetchone()[0]
            stage_n, task_n = db.execute(
                "SELECT count(*),count(CASE WHEN task=? THEN 1 END) FROM wires WHERE stage=?",
                (self.task_id, self.stage),
            ).fetchone()
            limit, per_task = LIMITS[self.stage]
            if stage_n >= limit or task_n >= per_task or all_cost + PER_REQUEST > cap:
                raise RuntimeError("Study budget or task dispatch limit reached")
            return db.execute(
                "INSERT INTO wires(stage,task,status,cost,local_input_estimate) "
                "VALUES (?,?,'unsettled',?,?)",
                (self.stage, self.task_id, PER_REQUEST, local_input_estimate),
            ).lastrowid

    async def acompletion(self, **kwargs):
        model = kwargs["model"].removeprefix("openai/").removeprefix("dashscope/")
        sdk = kwargs["client"]
        base = kwargs["api_base"].rstrip("/")
        if not (
            model == MODEL
            and _is_verified_aliyun_beijing_openai_endpoint(base)
            and str(sdk.base_url).rstrip("/") == base
            and sdk.max_retries == kwargs.get("num_retries", 0) == 0
            and not kwargs.get("stream")
            and type(kwargs.get("max_tokens")) is int
            and 0 < kwargs["max_tokens"] <= (256 if self.stage == "selector" else OUTPUT)
        ):
            raise ValueError("Route, model, stream, retry or output limit differs from protocol")
        fields = ("messages", "tools", "response_format", "max_tokens", "temperature", "extra_body")
        payload = {k: kwargs[k] for k in fields if k in kwargs}
        if payload.get("extra_body") not in (None, {"enable_thinking": False}):
            raise ValueError("Unfrozen provider options")
        # The provider owns its final template. The local estimate controls admission;
        # authorization still reserves the full token limits regardless of this estimate.
        estimate = input_bound(payload)
        if estimate > INPUT:
            raise ValueError("Serialized input exceeds conservative admission limit")
        wire = self.reserve(estimate)
        try:
            result = await sdk.chat.completions.create(model=model, **payload)
        except Exception as exc:
            # Even a known HTTP status need not establish whether the request was billed.
            with closing(sqlite3.connect(self.path)) as db, db:
                status = getattr(exc, "status_code", None)
                db.execute(
                    "UPDATE wires SET failure=?,http_status=? WHERE id=?",
                    (type(exc).__name__, status if type(status) is int else None, wire),
                )
            raise RuntimeError("Unsettled provider request; study stopped") from None
        if result.model != MODEL or not result.id or not result.usage:
            raise RuntimeError("Unconfirmed deployment identity or usage; study stopped")
        inp, out = result.usage.prompt_tokens, result.usage.completion_tokens
        if not (
            type(inp) is type(out) is int
            and 0 < inp <= INPUT
            and 0 <= out <= kwargs["max_tokens"]
            and result.usage.total_tokens == inp + out
        ):
            raise RuntimeError("Unconfirmed billing quantities; study stopped")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE wires SET status='returned',input=?,output=?,cost=?,provider_id=? "
                "WHERE id=?",
                (inp, out, inp * INPUT_PRICE + out * OUTPUT_PRICE, result.id, wire),
            )
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--cap-microcny", required=True, type=int)
    args = parser.parse_args()
    init_ledger(args.path, approval_id=args.approval_id, cap_microcny=args.cap_microcny)


if __name__ == "__main__":
    main()
