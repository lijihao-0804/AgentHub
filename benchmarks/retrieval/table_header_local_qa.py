"""Local language-model adapter and frozen scoring for the citation-QA route."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

import httpx

from packages.knowledge.citation_qa import _parse_model_output, _request, citation_system_prompt
from packages.model_gateway.contracts import ModelMessage, ModelResponse
from packages.model_gateway.credentials import (
    ProviderCredentialCipher,
    encrypt_provider_credential_secret,
)
from packages.model_gateway.models import ModelProfile, ProviderCredential

MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
WEIGHTS = "fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe"
GGUF_MODEL = "Qwen/Qwen2.5-3B-Instruct-GGUF"
GGUF_REVISION = "7dabda4d13d513e3e842b20f0d435c732f172cbe"
GGUF_WEIGHTS = "626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d"


def worked_example_prompt(mode, *, answer_contract_v1=False):
    """Research candidate; compose before the production context-budget check."""
    return citation_system_prompt(mode, answer_contract_v1=answer_contract_v1) + (
        "\nFor a supported answer, set insufficient_evidence=false. Put the evidence "
        "number in square brackets directly after its claim and list that same number "
        "in citation_ids. For example, if evidence 2 says the office opens on Monday, "
        'return {"answer":"The office opens on Monday [2].","citation_ids":[2],'
        '"insufficient_evidence":false}. Use the actual evidence numbers in this question. '
        "If the evidence cannot answer it, return "
        '{"answer":"Insufficient evidence.","citation_ids":[],"insufficient_evidence":true}.'
    )


def output_shape(content):
    try:
        return type(json.loads(content)).__name__
    except ValueError:
        return "markdown_fence" if content.lstrip().startswith("```") else "invalid_json"


class LocalGenerator:
    model_id = MODEL
    revision = REVISION
    context_size = 32768

    def __init__(self, path, device, mode):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        with (path / "model.safetensors").open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != WEIGHTS:
                raise ValueError("Unexpected local model weights.")
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
        self.model = (
            AutoModelForCausalLM.from_pretrained(
                path,
                local_files_only=True,
                torch_dtype=torch.float16 if device == "cuda" else torch.float32,
            )
            .to(device)
            .eval()
        )
        self.calls = 0
        self.output_shapes = []
        self.outputs = []
        self.mode = mode
        if mode == "JSON_SCHEMA":
            from lmformatenforcer.integrations.transformers import (
                build_token_enforcer_tokenizer_data,
            )

            self.token_data = build_token_enforcer_tokenizer_data(self.tokenizer)

    async def generate_resolved(self, context, plan, request):
        import torch

        self.calls += 1
        messages = [{"role": item.role, "content": item.content} for item in request.messages]
        inputs = self.tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_tensors="pt",
            return_dict=True,
        ).to(self.model.device)
        decoding = {}
        if request.response_schema is not None:
            from lmformatenforcer import JsonSchemaParser
            from lmformatenforcer.integrations.transformers import (
                build_transformers_prefix_allowed_tokens_fn,
            )

            decoding["prefix_allowed_tokens_fn"] = build_transformers_prefix_allowed_tokens_fn(
                self.token_data, JsonSchemaParser(dict(request.response_schema.json_schema))
            )
        with torch.inference_mode():
            tokens = self.model.generate(**inputs, **decoding, do_sample=False, max_new_tokens=256)
        completion = tokens[0, inputs["input_ids"].shape[-1] :]
        content = self.tokenizer.decode(completion, skip_special_tokens=True)
        self.output_shapes.append(output_shape(content))
        self.outputs.append(content)
        return ModelResponse(
            content=content,
            model=MODEL,
            provider="local-transformers",
            finish_reason="length" if len(completion) == 256 else "stop",
        )


class GGUFGenerator:
    model_id = GGUF_MODEL
    revision = GGUF_REVISION
    mode = "JSON_SCHEMA"
    context_size = 8192

    def __init__(self, path, short_schema=False):
        with path.open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != GGUF_WEIGHTS:
                raise ValueError("Unexpected GGUF weights.")
        self.calls = 0
        self.outputs = []
        self.output_shapes = []
        self.short_schema = short_schema

    async def generate_resolved(self, context, plan, request):
        self.calls += 1
        schema = request.response_schema
        assert schema is not None
        wire_schema = deepcopy(schema.json_schema)
        if self.short_schema:
            # b7550 rejects the 8,000-fold bounded string repetition. The tighter
            # decoder schema is a subset; production still validates its original.
            wire_schema["properties"]["answer"]["maxLength"] = min(
                256, wire_schema["properties"]["answer"]["maxLength"]
            )
        # Only the dedicated loopback server can receive evidence. No remote fallback.
        async with httpx.AsyncClient(
            trust_env=False, timeout=180, follow_redirects=False
        ) as client:
            response = await client.post(
                "http://127.0.0.1:56423/v1/chat/completions",
                json={
                    "model": GGUF_MODEL,
                    "temperature": 0,
                    "seed": 41,
                    "max_tokens": 256,
                    "messages": [{"role": m.role, "content": m.content} for m in request.messages],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema.name,
                            "strict": True,
                            "schema": wire_schema,
                        },
                    },
                },
            )
        response.raise_for_status()
        body = response.json()
        assert body["model"] == GGUF_MODEL
        choice = body["choices"][0]
        content = choice["message"]["content"]
        self.outputs.append(content)
        self.output_shapes.append(output_shape(content))
        return ModelResponse(
            content=content,
            model=GGUF_MODEL,
            provider="llama.cpp-b7550",
            finish_reason=choice["finish_reason"],
        )

    async def check_schema(self, server_log):
        offset = server_log.stat().st_size
        request = _request(
            (ModelMessage(role="user", content="Print only the unquoted word BANANA."),),
            "JSON_SCHEMA",
        )
        response = await self.generate_resolved(None, None, request)
        _parse_model_output(
            response.content, output_limit_reached=response.finish_reason == "length"
        )
        check_grammar_log(server_log, offset)
        return offset


def check_grammar_log(path, offset, expected_calls=1):
    requests = 0
    with path.open("rb") as file:
        file.seek(offset)
        for line in file:
            line = line.replace(b"\x00", b"")
            if b"failed to parse grammar" in line or b"error parsing grammar" in line:
                raise RuntimeError("Local server discarded the decoder grammar.")
            requests += b"POST /v1/chat/completions" in line
    if requests != expected_calls:
        raise RuntimeError("Local server log does not reconcile the measured request count.")


async def model_profile(factory, workspace_id, generator):
    async with factory() as session:
        credential = ProviderCredential(
            workspace_id=workspace_id, provider="offline", name="local-table-qa"
        )
        encrypt_provider_credential_secret(
            credential, "unused-offline-fixture", cipher=ProviderCredentialCipher.from_settings()
        )
        session.add(credential)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace_id,
            provider_credential_id=credential.id,
            model=generator.model_id,
            max_tokens=256,
            timeout_seconds=120,
            capabilities={
                "max_context_tokens": generator.context_size,
                "structured_output": generator.mode == "JSON_SCHEMA",
            },
        )
        session.add(profile)
        await session.commit()
        return str(profile.id)


def score(response, gold, citation):
    if response.status_code != 200:
        return {
            "http": response.status_code,
            "error": response.json()["error"]["code"],
            "reason": response.json()["error"]["message"],
            "refused": False,
            "value_correct": False,
            "cited_cell": False,
            "joint": False,
        }
    result = response.json()
    refused = result["insufficient_evidence"]
    answer = re.sub(r"\[\d+\]", "", result["answer"])
    if gold is None:
        value_ok = refused
        cited = not result["citations"]
    else:
        if gold.replace(",", "").isdigit():
            numbers = {n.replace(",", "") for n in re.findall(r"(?<!\w)\d[\d,]*(?:\.\d+)?", answer)}
            value_ok = numbers == {gold.replace(",", "")}
        else:
            # WAI poster controls. Reject competing values alongside the expected value.
            alternatives = (
                {"Zodiac", "Angels"}
                if gold == "Zodiac"
                else {"Sepia", "Full color", "Black and white"}
            )
            value_ok = {x for x in alternatives if x.lower() in answer.lower()} == {gold}
        value_ok = value_ok and not refused
        cited = False
        for item in result["citations"]:
            loc = item["locator"]
            for cell in loc["cells"]:
                if [loc["page"] - 1, cell["row"], cell["column"]] == citation and item["excerpt"][
                    cell["value_start"] : cell["char_end"]
                ] == gold:
                    cited = True
    return {
        "http": 200,
        "refused": refused,
        "value_correct": value_ok,
        "cited_cell": cited,
        "joint": value_ok and cited,
    }
