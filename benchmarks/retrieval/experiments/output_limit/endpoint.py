"""Replay fixed retrieval evidence through the citation QA HTTP route and production gateway."""

import asyncio
import hashlib
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import create_app
from apps.api.knowledge_dependencies import get_query_workspace_context, get_retrieval_components
from packages.core.config.settings import Settings
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge import citation_qa as qa
from packages.knowledge.composition import RetrievalComponents
from packages.knowledge.contracts import (
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceResult,
    RetrievalTraceStage,
    RetrievedEvidence,
)
from packages.model_gateway.adapters.litellm.provider_http import GuardedProviderTransport
from packages.model_gateway.credentials import ProviderCredentialCipher

MODEL = "deepseek-v4.1-flash-1017"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PATH = "/api/v1/knowledge/query"


def digest(data):
    return hashlib.sha256(
        json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def frozen_retrieval(case, snapshot):
    """Rehydrate the exact numbered records observed after the archived selection."""
    evidence = []
    prompt = case["messages"][1]["content"]
    for number, row in enumerate(case["selected"], 1):
        if row["id"] != number:
            raise ValueError("Frozen evidence numbering drift")
        locator = json.dumps(
            row["locator"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        prefix = f"\n[{number}]\nsource: {row['source']}\nlocator: {locator}\n"
        start = prompt.find(prefix)
        if start < 0:
            raise ValueError("Frozen locator or source drift")
        lifecycle = re.match(r"document_lifecycle: ([^\n]+)\n", prompt[start + len(prefix) :])
        metadata = json.loads(lifecycle[1]) if lifecycle else {}
        if "metadata" in row and any(row["metadata"].get(k) != v for k, v in metadata.items()):
            raise ValueError("Frozen lifecycle drift")
        evidence.append(
            RetrievedEvidence(
                row["document"],
                row["revision"],
                row["chunk_id"],
                row["source"],
                row["locator"],
                row["text"],
                row["retrieval_score"],
                row["rerank_score"],
                metadata,
            )
        )
    selected = qa._select_evidence(tuple(evidence), max_chars=16000)
    messages = [
        {"role": "system", "content": qa.citation_system_prompt("PROMPT_JSON")},
        {"role": "user", "content": qa._evidence_prompt(case["question"], selected)},
    ]
    if messages != case["messages"] or not selected:
        raise ValueError("Frozen messages differ from the production renderer")
    empty = RetrievalTraceStage(0, ())
    ranking = RetrievalTraceStage(
        0,
        tuple(
            RetrievalTraceResult(r.chunk_id, i, r.retrieval_score)
            for i, r in enumerate(evidence, 1)
        ),
    )
    return RetrievalResult(
        tuple(evidence), RetrievalTrace(str(snapshot), empty, empty, empty, ranking, 0)
    )


def historical_failures(repo):
    """Retain each failed *historical arm*, even for an identical question or prompt."""
    from .cases import history_case_index

    archive = Path(repo) / ".scratch/joint-six-qa-20261118/paid-formal/fresh.jsonl"
    raw = archive.read_bytes()
    if (
        hashlib.sha256(raw).hexdigest()
        != "88c13b5783f2aef98b7bae1c4e31cb4084ce4d470edef313cd4c3b11d454b37b"
    ):
        raise ValueError("Historical paid new-question cohort changed")
    raw_ledger = archive.with_name("ledger.jsonl").read_bytes()
    if (
        hashlib.sha256(raw_ledger).hexdigest()
        != "0d5b3bb1fc3fb233981d4d2311bf044bff346bef9f49b06059ae7ec09d144510"
    ):
        raise ValueError("Historical provider receipts changed")
    receipts = {}
    for line in raw_ledger.splitlines():
        entry = json.loads(line)
        if entry["state"] == "confirmed":
            receipts[entry["id"]] = entry
    index = history_case_index(repo)
    failures = []
    for line in raw.splitlines():
        row = json.loads(line)
        if row["http"] != 502:
            continue
        if len(row["events"]) != 1 or row["events"][0]["finish"] != "length":
            raise ValueError("Historical arm did not end on its first capped response")
        event = row["events"][0]
        empty = not (event["content"] or "").strip()
        code = "MODEL_BAD_RESPONSE" if empty else "CITATION_QA_INVALID_MODEL_OUTPUT"
        if code != row["body"]["error"]["code"]:
            raise ValueError("Historical error classification changed")
        usage = receipts[event["id"]]["usage"]
        details = usage["completion_tokens_details"]
        if usage["completion_tokens"] != 256 or empty != (details["text_tokens"] == 0):
            raise ValueError("Archived output-limit accounting changed")
        qid, arm = row["id"], row["arm"]
        failures.append(
            dict(
                index["fresh", qid, arm],
                id=f"historical/{qid}/{arm}",
                qid=qid,
                original_id=qid,
                historical_arm=arm,
                cohort="historical",
                failure_class="empty" if empty else "truncated",
                historical_http=502,
                historical_reasoning_tokens=details["reasoning_tokens"],
                historical_text_tokens=details["text_tokens"],
                historical_receipt_id=event["id"],
                protected=False,
            )
        )
    if len(failures) != 57 or sum(r["failure_class"] == "empty" for r in failures) != 39:
        raise ValueError("Historical 39/18 failure strata changed")
    return failures


def original_binding(case):
    if case.get("kb") and case.get("snapshot"):
        return {"kb": case["kb"], "snapshot": case["snapshot"]}
    original = case.get("original_id", case.get("qid", case["id"]))
    folder = Path(__file__).resolve().parents[4] / ".scratch/joint-six-qa-20261118/inputs"
    for group in ("fresh", "B", "R", "W"):
        path = folder / group / "bindings.json"
        if path.is_file() and (row := json.loads(path.read_bytes())["bindings"].get(original)):
            return row
    if case.get("cohort") != "fresh":
        raise ValueError(f"No archived KB/snapshot binding for {original}")
    return {
        "kb": str(uuid5(NAMESPACE_URL, original + "/kb")),
        "snapshot": str(uuid5(NAMESPACE_URL, original + "/snapshot")),
    }


class ReplayEndpoint:
    """Serial, process-local harness. No external dispatch when meter is absent."""

    def __init__(self, route, meter=None):
        self.base_url = route.base_url.rstrip("/")
        verified_route = (
            hashlib.sha256(self.base_url.encode()).hexdigest()
            == "b2b825819665b9d013746c99ebdf2a3bbac6ca6a558b2d3b9da6daf1839404a5"
            and digest(self.base_url)
            == "7b2e589adae515d795a2bdc1219b2d4c989477124f371d56d77d97ddefcb4d7e"
        )
        offline_route = meter is None and self.base_url == BASE_URL
        if (
            route.model != MODEL
            or route.provider != "openai-compatible"
            or not (verified_route or offline_route)
        ):
            raise ValueError("Unexpected provider route")
        self.route, self.meter = route, meter
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        self.engine = create_engine("sqlite://")
        self.client = None
        try:
            settings = Settings(
                _env_file=None,
                credential_master_key=secrets.token_urlsafe(32),
                knowledge_qa_max_evidence_chars=16000,
                knowledge_qa_contextual_abstention=False,
                knowledge_qa_answer_contract_v1=False,
            )
            workspace, credential = uuid4(), uuid4()
            self.profiles = {cap: uuid4() for cap in (256, 2048)}
            # Only the two gateway tables are needed. Use SQLite-compatible DDL for
            # their PostgreSQL JSONB default; real ORM reads, route resolution and
            # credential decryption run in the production repository.
            with self.engine.begin() as db:
                db.exec_driver_sql("""CREATE TABLE provider_credentials (
                    id TEXT PRIMARY KEY, workspace_id TEXT, provider TEXT, name TEXT, secret TEXT,
                    secret_ciphertext TEXT, secret_version INTEGER, base_url TEXT, enabled BOOLEAN,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
                db.exec_driver_sql("""CREATE TABLE model_profiles (
                    id TEXT PRIMARY KEY, workspace_id TEXT, provider_credential_id TEXT, model TEXT,
                    temperature NUMERIC, max_tokens INTEGER, timeout_seconds NUMERIC,
                    fallback_profile_id TEXT, capabilities JSON, enabled BOOLEAN,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
                secret = ProviderCredentialCipher.from_settings(settings).encrypt(
                    self.route.api_key
                )
                db.exec_driver_sql(
                    """INSERT INTO provider_credentials
                    (id,workspace_id,provider,name,secret_ciphertext,secret_version,base_url,enabled)
                    VALUES(?,?,?,'benchmark',?,1,?,1)""",
                    (credential.hex, workspace.hex, self.route.provider, secret, self.base_url),
                )
                for cap, profile in self.profiles.items():
                    db.exec_driver_sql(
                        """INSERT INTO model_profiles
                        (id,workspace_id,provider_credential_id,model,temperature,max_tokens,
                         timeout_seconds,capabilities,enabled) VALUES(?,?,?,?,0,?,120,?,1)""",
                        (
                            profile.hex,
                            workspace.hex,
                            credential.hex,
                            MODEL,
                            cap,
                            '{"max_context_tokens":32768,"structured_output":false}',
                        ),
                    )
            self.context = WorkspaceExecutionContext.model_validate(
                {
                    "organization": {
                        "principal": {"request_id": "output-limit", "trace_id": "output-limit"},
                        "organization_id": str(uuid4()),
                        "org_role": "OWNER",
                    },
                    "workspace_id": str(workspace),
                    "workspace_role": "OWNER",
                    "permissions": ["knowledge_run"],
                }
            )

            @asynccontextmanager
            async def session_factory():
                with Session(self.engine) as session:

                    class LocalSession:
                        async def execute(self, statement):
                            return session.execute(statement)

                    yield LocalSession()

            app = create_app(settings)
            app.state.db_session_factory = session_factory
            app.dependency_overrides[get_query_workspace_context] = lambda: self.context
            app.dependency_overrides[get_retrieval_components] = lambda: RetrievalComponents(
                None, None, None, None
            )
            self.client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://benchmark.invalid"
            )
            return self
        except BaseException:
            self.engine.dispose()
            raise

    async def __aexit__(self, *args):
        try:
            await self.client.aclose()
        finally:
            self.engine.dispose()

    async def query(self, case, max_tokens):
        async with self.lock:
            return await self._query(case, max_tokens)

    async def _query(self, case, cap):
        profile = self.profiles[cap]
        binding = original_binding(case)
        snapshot = binding["snapshot"]
        result = frozen_retrieval(case, snapshot)
        events, errors = [], []
        original = GuardedProviderTransport.handle_async_request

        async def retrieve(*args, **kwargs):
            return result

        async def guarded(transport, request):
            try:
                payload = json.loads(request.content)
                if not events and case.get("first_request_sha256"):
                    # Replace only the top-level number, preserving the serializer's
                    # ordering, unicode escapes and every other physical byte.
                    original_bytes, changed = re.subn(
                        rb'(?<!\\)("max_tokens"\s*:\s*)' + str(cap).encode() + rb"(?=\s*[,}])",
                        lambda match: match[1] + b"256",
                        request.content,
                    )
                    if (
                        changed != 1
                        or hashlib.sha256(original_bytes).hexdigest()
                        != case["first_request_sha256"]
                    ):
                        raise ValueError("Physical input bytes differ from archived request")
                msgs = payload.pop("messages")
                expected = {"model": MODEL, "max_tokens": cap, "temperature": 0.0}
                if (
                    digest(expected) != digest(payload)
                    or str(request.url) != self.base_url + "/chat/completions"
                    or request.method != "POST"
                    or errors
                    or len(events) >= 2
                    or msgs[:2] != case["messages"]
                ):
                    raise ValueError("Physical request differs from frozen controls")
                attempt = len(events) + 1
                expected_wire = case.get("registered_wire")
                if (
                    attempt == 1
                    and expected_wire
                    and hashlib.sha256(request.content).hexdigest() != expected_wire
                ):
                    raise ValueError("First physical request differs from local preflight")
                if attempt == 1 and len(msgs) != 2:
                    raise ValueError("Unexpected initial messages")
                if attempt == 2 and not (
                    len(msgs) == 4
                    and msgs[2] == {"role": "assistant", "content": events[0]["content"]}
                    and msgs[3]["role"] == "user"
                    and msgs[3]["content"]
                    in (qa._CITATION_CORRECTION, qa._PROMPT_CITATION_CORRECTION)
                ):
                    raise ValueError("Unexpected correction")

                async def send():
                    response = await original(transport, request)
                    try:
                        await response.aread()
                        if not response.is_success:
                            raise RuntimeError(f"Provider returned HTTP {response.status_code}")
                        return response.json()
                    finally:
                        await response.aclose()

                started = perf_counter()
                key = f"{case['id']}/{cap}/{attempt}"
                if self.meter is None:
                    body = {
                        "id": "offline-" + key,
                        "model": MODEL,
                        "created": 1,
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        {
                                            "answer": "Evidence unavailable.",
                                            "citation_ids": [],
                                            "insufficient_evidence": True,
                                        }
                                    ),
                                },
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 40,
                            "completion_tokens": 20,
                            "total_tokens": 60,
                            "prompt_tokens_details": {"cached_tokens": 0},
                        },
                    }
                else:
                    body = await self.meter.arun(key, case.get("qid", case["id"]), msgs, cap, send)
                choice = body["choices"][0]
                events.append(
                    dict(
                        content=choice["message"].get("content"),
                        finish=choice["finish_reason"],
                        id=body["id"],
                        created=body["created"],
                        usage=body["usage"],
                        elapsed_ms=(perf_counter() - started) * 1000,
                        messages_sha256=digest(msgs),
                        parameters_sha256=digest(payload),
                        request_sha256=hashlib.sha256(request.content).hexdigest(),
                    )
                )
                return httpx.Response(200, json=body, request=request)
            except BaseException as exc:
                errors.append(exc)
                raise

        with (
            patch.object(qa.HybridKnowledgeRetriever, "retrieve_with_trace", retrieve),
            patch.object(GuardedProviderTransport, "handle_async_request", guarded),
        ):
            response = await self.client.post(
                PATH,
                json=dict(
                    workspace_id=self.context.workspace_id,
                    knowledge_base_id=binding["kb"],
                    knowledge_snapshot_id=str(snapshot),
                    model_profile_id=str(profile),
                    query=case["question"],
                    output_mode="PROMPT_JSON",
                ),
            )
        if errors:
            raise errors[0]
        if not events:
            raise RuntimeError(
                f"Endpoint stopped before wire: HTTP {response.status_code} {response.text[:300]}"
            )
        return response, events
