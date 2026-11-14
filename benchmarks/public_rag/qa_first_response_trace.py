"""Read-only telemetry for serial public-data benchmark requests in a loopback API."""

import hashlib
import json
import time
from pathlib import Path


def install(path: Path):
    from packages.knowledge import citation_qa as qa
    from packages.model_gateway.adapters.litellm import adapter
    from packages.model_gateway.errors import ModelGatewayError

    def event(kind, **data):
        with path.open("a", encoding="utf8") as out:
            out.write(json.dumps(dict(kind=kind, **data), allow_nan=False) + "\n")

    retrieve = qa.HybridKnowledgeRetriever.retrieve_with_trace

    async def observed_retrieve(self, *args, **kwargs):
        result = await retrieve(self, *args, **kwargs)
        trace = result.trace
        stage = trace.selection if trace.selection is not None else trace.fusion
        event(
            "retrieval",
            snapshot=trace.snapshot_id,
            candidate=[item.chunk_id for item in stage.results][:20],
            reranked=[item.chunk_id for item in result.evidence],
            times_ms={
                k: getattr(trace, k).latency_ms for k in ("dense", "sparse", "fusion", "rerank")
            },
            total_ms=trace.total_latency_ms,
        )
        return result

    render = qa._evidence_prompt

    def observed_prompt(query, selected):
        value = render(query, selected)
        event(
            "reader",
            prompt_sha256=hashlib.sha256(value.encode()).hexdigest(),
            chunks=[item.evidence.chunk_id for item in selected],
        )
        return value

    build = adapter._request_payload

    def observed_payload(profile, credential, request, **kwargs):
        value = build(profile, credential, request, **kwargs)
        # Only allowlisted operational fields and digests; never persist messages or credentials.
        event(
            "wire",
            max_tokens=value["max_tokens"],
            timeout_seconds=value["timeout"],
            system_prompt_sha256=hashlib.sha256(value["messages"][0]["content"].encode()).hexdigest(),
            temperature=value["temperature"],
            model=profile.model,
            retry_limit=value["num_retries"],
            enable_thinking=value.get("extra_body", {}).get("enable_thinking"),
            prompt_sha256=hashlib.sha256(value["messages"][1]["content"].encode()).hexdigest(),
        )
        return value

    complete = adapter.LiteLLMProviderAdapter.complete

    async def observed_complete(self, *args, **kwargs):
        start = time.perf_counter()
        try:
            response = await complete(self, *args, **kwargs)
        except Exception as exc:
            event(
                "adapter_error",
                code=exc.code.value if isinstance(exc, ModelGatewayError) else "other",
                elapsed_ms=round(1000 * (time.perf_counter() - start)),
            )
            raise
        usage = response.usage
        event(
            "response",
            elapsed_ms=round(1000 * (time.perf_counter() - start)),
            finish_reason=response.finish_reason
            if response.finish_reason
            in {None, "stop", "length", "tool_calls", "content_filter", "refusal"}
            else "other",
            content_present=bool(response.content.strip()),
            usage={
                k: getattr(usage, k, None)
                for k in ("input_tokens", "output_tokens", "total_tokens")
            }
            if usage
            else None,
        )
        return response

    qa.HybridKnowledgeRetriever.retrieve_with_trace = observed_retrieve
    qa._evidence_prompt = observed_prompt
    adapter._request_payload = observed_payload
    adapter.LiteLLMProviderAdapter.complete = observed_complete
