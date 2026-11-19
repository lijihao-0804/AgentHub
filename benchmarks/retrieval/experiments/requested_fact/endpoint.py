"""Measure the two mainline API contracts through the guarded 2048-token gateway."""

import json
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

from ..first_refusal_contract.endpoint import FIRST_REFUSAL, WIRE, PairedEndpoint, original
from ..refusal_prompt.endpoint import SETTING as OLD_PROMPT

SETTING = "knowledge_qa_requested_fact_contract"


def arm_case(case, arm):
    base = original.qa.citation_system_prompt("PROMPT_JSON")
    if arm not in ("baseline", "candidate") or case["messages"][0] != {
        "role": "system",
        "content": base,
    }:
        raise ValueError("Invalid mainline system input")
    if arm == "baseline":
        return dict(case)
    system = original.qa.citation_system_prompt("PROMPT_JSON", requested_fact_contract=True)
    if system == base:
        raise ValueError("Requested-fact prompt not enabled")
    row = dict(case, messages=[{"role": "system", "content": system}, case["messages"][1]])
    row.pop("first_request_sha256", None)
    row.pop("registered_wire", None)
    return row


class DryTransport:
    """A valid, inert response for the exact output schema of the current treatment."""

    def __init__(self, arm):
        self.arm = arm
        self.notes = []

    async def arun(self, key, qid, messages, cap, send):
        obj = (
            {"kind": "insufficient"}
            if self.arm == "candidate"
            else {
                "answer": "Evidence unavailable.",
                "citation_ids": [],
                "insufficient_evidence": True,
            }
        )
        self.notes.append(dict(wire=WIRE.get(), physical_key=key, provenance="inert"))
        return dict(
            id="dry-" + self.arm + "-" + key,
            model=original.MODEL,
            object="chat.completion",
            created=1,
            choices=[
                dict(
                    index=0,
                    finish_reason="stop",
                    message=dict(role="assistant", content=json.dumps(obj)),
                )
            ],
            usage=dict(
                prompt_tokens=100,
                completion_tokens=15,
                total_tokens=115,
                prompt_tokens_details=dict(cached_tokens=0),
            ),
        )


class FactEndpoint(PairedEndpoint):
    def __init__(self, route, treatment, meter=None):
        super().__init__(route, "baseline", meter)
        self.treatment = treatment

    async def __aenter__(self):
        if {FIRST_REFUSAL, OLD_PROMPT, SETTING} - original.Settings.model_fields.keys():
            raise ValueError("Missing requested-fact product setting")
        with patch.object(
            original,
            "Settings",
            partial(
                original.Settings,
                **{FIRST_REFUSAL: False, OLD_PROMPT: False, SETTING: self.treatment == "candidate"},
            ),
        ):
            return await original.ReplayEndpoint.__aenter__(self)

    async def _query(self, case, cap):
        if self.treatment == "baseline":
            return await super()._query(case, cap)
        # Only the legacy wire harness gets a different correction whitelist.
        # The production service retains its actual module and prompt constants.
        allowed = SimpleNamespace(**vars(original.qa))
        allowed._CITATION_CORRECTION = allowed._PROMPT_CITATION_CORRECTION = (
            original.qa._REQUESTED_FACT_CORRECTION
        )
        with patch.object(original, "qa", allowed):
            return await super()._query(case, cap)

    async def measure(self, case, wire=None):
        prepared = arm_case(case, self.treatment)
        if wire:
            prepared["registered_wire"] = wire
        frozen = original.frozen_retrieval
        qa = original.qa
        resolver = qa.resolve_citation_answer
        parsed = []

        def observe(query, content, **kwargs):
            if kwargs.get("requested_fact_contract", False) is not (self.treatment == "candidate"):
                raise AssertionError("Parser treatment differs from the registered product setting")
            if kwargs.get("answer_contract_v1") or kwargs.get("first_refusal_contract"):
                raise AssertionError("Other experimental contracts enabled")
            try:
                receipt = resolver(query, content, **kwargs)
            except qa._InvalidCitationAnswer as exc:
                parsed.append(dict(raw=content, valid=False, reason=exc.reason.value, rule=None))
                raise
            parsed.append(dict(raw=content, valid=True, reason=None, rule=receipt.rule))
            return receipt

        with (
            patch.object(original, "frozen_retrieval", lambda _, snap: frozen(case, snap)),
            patch.object(qa, "resolve_citation_answer", observe),
        ):
            out = await self.exchange(prepared)
        # Preserve the actual attempt position even if the gateway returned no
        # visible text before a subsequent valid parse.
        seq = iter(parsed)
        for event in out["events"]:
            content = event["content"]
            if not (content or "").strip():
                event["parser"] = dict(valid=False, reason="empty_output", rule=None)
                continue
            result = next(seq)
            if result.pop("raw") != content:
                raise ValueError("Observed parser and provider event differ")
            event["parser"] = result
        if next(seq, None) is not None:
            raise ValueError("Surplus parser observations")
        first = out["events"][0]["parser"]
        if not first["valid"]:
            out["first"] = dict(first, refused=False, answer="", cited=[])
        if (out["first"]["valid"], out["first"]["rule"], out["first"]["reason"]) != (
            first["valid"],
            first["rule"],
            first["reason"],
        ):
            raise ValueError("First answer differs from the first physical attempt")
        out["arm"] = self.treatment
        return out
