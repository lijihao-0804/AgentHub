"""Observe both first-answer contracts at the real API, sharing only identical wires."""

import hashlib
import json
from contextvars import ContextVar
from functools import partial
from time import perf_counter
from unittest.mock import patch

import httpx

from ..output_limit import endpoint as original
from ..output_limit.cases import digest

FIRST_REFUSAL = "knowledge_qa_first_refusal_contract"
WIRE = ContextVar("paired_physical_request")
ARMS = ("baseline", "candidate")


class PairedEndpoint(original.ReplayEndpoint):
    def __init__(self, route, arm, meter=None):
        super().__init__(route, meter)
        self.arm = arm

    async def __aenter__(self):
        if FIRST_REFUSAL not in original.Settings.model_fields or self.arm not in ARMS:
            raise ValueError("Registered product setting unavailable")
        with patch.object(
            original,
            "Settings",
            partial(original.Settings, **{FIRST_REFUSAL: self.arm == "candidate"}),
        ):
            return await super().__aenter__()

    async def exchange(self, case):
        parsed = []
        resolver = original.qa.resolve_citation_answer
        http_send = httpx.AsyncClient._send_single_request

        def observe(query, content, **kwargs):
            if kwargs.get("first_refusal_contract") is not (self.arm == "candidate" and not parsed):
                raise AssertionError("Product did not evaluate the registered arm")
            try:
                rec = resolver(query, content, **kwargs)
            except original.qa._InvalidCitationAnswer as exc:
                parsed.append(
                    dict(
                        valid=False,
                        refused=False,
                        answer="",
                        cited=[],
                        reason=exc.reason.value,
                        rule=None,
                    )
                )
                raise
            visible = {s["id"]: s for s in case["selected"]}
            cited = [
                dict(
                    id=i,
                    text=visible[i]["text"],
                    source=visible[i]["source_id"],
                    locator=visible[i]["locator"],
                )
                for i in rec.citation_ids
            ]
            parsed.append(
                dict(
                    valid=True,
                    refused=rec.insufficient_evidence,
                    answer=rec.answer,
                    cited=cited,
                    reason=None,
                    rule=rec.rule,
                )
            )
            return rec

        async def track_wire(client, request):
            # Observe bytes before the output_limit endpoint's guarded transport. The
            # original transport still validates the route, model, payload and turns.
            if str(request.url) == self.base_url + "/chat/completions":
                token = WIRE.set(hashlib.sha256(request.content).hexdigest())
                try:
                    return await http_send(client, request)
                finally:
                    WIRE.reset(token)
            return await http_send(client, request)

        started = perf_counter()
        with (
            patch.object(original.qa, "resolve_citation_answer", observe),
            patch.object(httpx.AsyncClient, "_send_single_request", track_wire),
        ):
            response, events = await self.query(case, 2048)
        body = response.json()
        if response.status_code != 200 and not (
            response.status_code == 502
            and body.get("error", {}).get("code")
            in {"MODEL_BAD_RESPONSE", "CITATION_QA_INVALID_MODEL_OUTPUT"}
        ):
            raise RuntimeError(f"Unexpected product HTTP {response.status_code}")
        if len(parsed) != sum(bool((e["content"] or "").strip()) for e in events):
            raise ValueError("Unexpected number of observed parser results")
        first = (
            parsed[0]
            if parsed
            else dict(
                valid=False, refused=False, answer="", cited=[], reason="empty_output", rule=None
            )
        )
        if response.status_code == 200:
            p = parsed[-1]
            if (p["valid"], p["answer"], p["refused"], [v["id"] for v in p["cited"]]) != (
                True,
                body["answer"],
                body["insufficient_evidence"],
                [v["id"] for v in body["citations"]],
            ):
                raise ValueError("Final API answer differs from the observed product receipt")
            for citation in body["citations"]:
                item = case["selected"][citation["id"] - 1]
                if (
                    not citation["excerpt"]
                    or citation["excerpt"] != item["text"][: original.qa.MAX_EVIDENCE_EXCERPT_CHARS]
                    or any(
                        citation[k] != item[j]
                        for k, j in (
                            ("document_id", "document"),
                            ("document_revision_id", "revision"),
                            ("chunk_id", "chunk_id"),
                            ("source", "source"),
                            ("locator", "locator"),
                        )
                    )
                ):
                    raise ValueError("Unbound API citation")
        if self.meter:
            for event, note in zip(events, self.meter.notes, strict=True):
                if event["request_sha256"] != note["wire"]:
                    raise ValueError("Observed HTTP bytes differ at the transport boundary")
                event.update(note)
        return dict(
            id=case["id"],
            qid=case["qid"],
            cohort=case["cohort"],
            mode=case["mode"],
            arm=self.arm,
            cap=2048,
            http=response.status_code,
            body=body,
            events=events,
            first=first,
            elapsed_ms=(perf_counter() - started) * 1000,
        )


class PairedTransport:
    def __init__(self, previous, ledger=None):
        self.previous = previous
        self.ledger = ledger

    def select(self, case, archived):
        self.case, self.archived, self.notes = case, archived, []

    async def arun(self, key, qid, messages, cap, send):
        case = self.case
        wire = WIRE.get()
        n = len(self.notes) + 1
        if key != f"{case['id']}/2048/{n}" or qid != case["qid"] or cap != 2048:
            raise ValueError("Invalid attempt key")
        if case["mode"] == "archive":
            event = self.archived["events"][n - 1]
            receipt = self.previous[event["id"]]
            if (wire, digest(messages), receipt["digest"], receipt["qid"]) != (
                event["request_sha256"],
                event["messages_sha256"],
                event["messages_sha256"],
                qid,
            ):
                raise ValueError("Archive input bytes do not match this prior attempt")
            body = json.loads(receipt["response"])
            physical_key, source = receipt["key"], "archived_receipt"
        else:
            if self.ledger is None:
                raise ValueError("No paid transport available")
            physical_key = "paired/" + key + "/" + wire
            body = self.ledger.reserve(physical_key, qid, messages, cap)
            source = "confirmed_reuse"
            if body is None:
                body = self.ledger.settle(physical_key, await send())
                source = "provider"
        self.notes.append(dict(wire=wire, physical_key=physical_key, provenance=source))
        return body
