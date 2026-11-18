"""Private serial wire ledger for the Beijing output-limit campaign."""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

MODEL = "deepseek-v4.1-flash-1017"
# Units are 1e-8 CNY. All calculations are integral, including cache hits.
OPENING = 209673672
CAP = 3000000000
BLOCKED_QIDS = {"table_header_answers/baseline/1", "2hop__75213_24779"}
BLOCKED_MESSAGES = {
    "cc7dd6ec4adc884a218a6bc95540fbae59bc4ba5bd75a39d92a282dbed614dd2",
    "32716c315319949eaf723ce1bde6866f5102ec9023092254a66aaa087e3f5516",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@lru_cache(maxsize=1)
def historical_ids():
    scratch = Path(__file__).resolve().parents[4] / ".scratch"
    ids = set()
    for name in (
        "first-refusal-100-20261118/independent",
        "bilingual-refusal-20261118/independent",
        "bilingual-refusal-20261118/old100-live",
        "bilingual-refusal-20261118/wai14-live",
    ):
        for line in (scratch / name / "wire-private.jsonl").read_bytes().splitlines():
            ids.add(json.loads(line)["provider_response"]["id"])
    for stage in ("smoke", "formal"):
        path = scratch / "joint-six-qa-20261118" / ("paid-" + stage) / "ledger.jsonl"
        for line in path.read_bytes().splitlines():
            row = json.loads(line)
            if row["state"] == "confirmed":
                ids.add(row["id"])
    if len(ids) != 956:
        raise ValueError("historical receipt set incomplete")
    return frozenset(ids)


class Meter:
    def __init__(self, path):
        self.path = path
        self.prior_ids = historical_ids()
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                key TEXT PRIMARY KEY, qid TEXT NOT NULL, digest TEXT NOT NULL,
                cap INTEGER NOT NULL, bound INTEGER NOT NULL, reserve INTEGER NOT NULL,
                cost INTEGER, response_id TEXT UNIQUE, response TEXT)""")
            db.execute("CREATE TABLE IF NOT EXISTS invalid_answers (key TEXT PRIMARY KEY)")

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    def reserve(self, key, qid, messages, max_tokens):
        if not key or not qid or type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("invalid physical request identity or limit")
        raw = canonical(messages)
        digest = hashlib.sha256(raw).hexdigest()
        if (
            qid in BLOCKED_QIDS
            or hashlib.sha256(canonical(messages[:2])).hexdigest() in BLOCKED_MESSAGES
        ):
            raise ValueError("historically unresolved request")
        bound = len(raw) + 256 + 64 * len(messages)
        reserve = 200 * bound + 800 * max_tokens
        with self.connection() as db:
            if db.execute("SELECT 1 FROM invalid_answers").fetchone():
                raise RuntimeError("NEEDS_ATTENTION: confirmed usage with invalid answer")
            if db.execute("SELECT 1 FROM calls WHERE cost IS NULL").fetchone():
                raise RuntimeError("NEEDS_ATTENTION: unresolved physical reservation")
            prior = db.execute(
                "SELECT qid,digest,cap,response FROM calls WHERE key=?", (key,)
            ).fetchone()
            if prior:
                if prior[:3] != (qid, digest, max_tokens):
                    raise ValueError("physical key reused with changed request")
                return json.loads(prior[3])
            spent = db.execute("SELECT COALESCE(SUM(cost),0) FROM calls").fetchone()[0]
            if OPENING + spent + reserve > CAP:
                raise RuntimeError("campaign budget exhausted")
            db.execute(
                "INSERT INTO calls(key,qid,digest,cap,bound,reserve) VALUES (?,?,?,?,?,?)",
                (key, qid, digest, max_tokens, bound, reserve),
            )
        return None

    def settle(self, key, response):
        if response.get("id") in self.prior_ids:
            raise RuntimeError("duplicate historical receipt")
        u = response["usage"]
        i, o, c = (
            u["prompt_tokens"],
            u["completion_tokens"],
            u["prompt_tokens_details"]["cached_tokens"],
        )
        if (
            response["model"] != MODEL
            or not isinstance(response["id"], str)
            or not response["id"]
            or not all(type(v) is int for v in (i, o, c, response["created"], u["total_tokens"]))
            or not 0 <= c <= i
            or not i
            or o < 0
            or i + o != u["total_tokens"]
        ):
            raise RuntimeError("invalid provider receipt")
        hour = datetime.fromtimestamp(response["created"], timezone(timedelta(hours=8))).hour
        cost = ((i - c) * 100 + c * 2 + o * 400) * (2 if 8 <= hour < 22 else 1)
        public = {k: response[k] for k in ("id", "model", "created", "object") if k in response}
        public["usage"] = {
            "prompt_tokens": i,
            "completion_tokens": o,
            "total_tokens": i + o,
            "prompt_tokens_details": {"cached_tokens": c},
        }
        details = u.get("completion_tokens_details")
        if not isinstance(details, dict):
            details = {}
        public["usage"]["completion_tokens_details"] = {
            k: details[k]
            for k in ("reasoning_tokens", "text_tokens")
            if type(details.get(k)) is int
        }
        choices = response.get("choices")
        choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else None
        message = choice.get("message") if isinstance(choice, dict) else None
        valid = (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and isinstance(message.get("content"), str)
            and choice.get("index") == 0
            and isinstance(choice.get("finish_reason"), str)
        )
        public["choices"] = []
        if valid:
            public["choices"].append(
                {
                    "index": 0,
                    "finish_reason": choice["finish_reason"],
                    "message": {"role": "assistant", "content": message["content"]},
                }
            )
        with self.connection() as db:
            row = db.execute(
                "SELECT bound,cap FROM calls WHERE key=? AND cost IS NULL", (key,)
            ).fetchone()
            if not row or i > row[0] or o > row[1]:
                raise RuntimeError("receipt outside physical reservation")
            db.execute(
                "UPDATE calls SET cost=?,response_id=?,response=? WHERE key=?",
                (cost, response["id"], json.dumps(public, ensure_ascii=False), key),
            )
            if not valid:
                db.execute("INSERT INTO invalid_answers VALUES (?)", (key,))
        if not valid:
            raise RuntimeError("NEEDS_ATTENTION: confirmed usage with invalid answer")
        return public

    def run(self, key, qid, messages, max_tokens, send):
        cached = self.reserve(key, qid, messages, max_tokens)
        if cached is not None:
            return cached
        return self.settle(key, send())

    async def arun(self, key, qid, messages, max_tokens, send):
        cached = self.reserve(key, qid, messages, max_tokens)
        if cached is not None:
            return cached
        return self.settle(key, await send())
