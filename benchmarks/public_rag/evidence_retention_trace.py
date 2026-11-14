"""Instrument a loopback benchmark API with two rerankers on the same real components."""

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from benchmarks.public_rag.qa_first_response_trace import install as install_common_trace


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def instrument(app, path):
    from fastapi import Request

    from apps.api.knowledge_dependencies import get_retrieval_components
    from packages.core.config.settings import get_settings
    from packages.knowledge import citation_qa as qa
    from packages.model_gateway.adapters.litellm.provider_http import GuardedProviderTransport

    def event(kind, **fields):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, **fields}, allow_nan=False) + "\n")

    install_common_trace(path)
    render = qa._evidence_prompt

    def observed_render(query, selected):
        prompt = render(query, selected)
        entries = []
        for numbered in selected:
            record = numbered.evidence
            lifecycle = {
                k: record.metadata[k]
                for k in ("effective_date", "superseded")
                if record.metadata.get(k) is not None
            }
            entries.append(
                {
                    "number": numbered.id,
                    "chunk_id": record.chunk_id,
                    "document_id": record.document_id,
                    "document_revision_id": record.document_revision_id,
                    "source": record.source,
                    "locator": record.locator,
                    "lifecycle": lifecycle,
                    "text": numbered.text,
                }
            )
        event("reader_entries", prompt_sha256=digest(prompt), entries=entries)
        return prompt

    qa._evidence_prompt = observed_render
    transport = GuardedProviderTransport.handle_async_request

    async def observed_http(self, request):
        # Only the benchmark's public queries use this process. Never persist headers or secrets.
        body = json.loads(await request.aread())
        messages = body["messages"]
        event(
            "http_wire",
            method=request.method,
            model=body["model"],
            max_tokens=body.get("max_tokens", body.get("max_completion_tokens")),
            temperature=body["temperature"],
            thinking_override_present="enable_thinking" in body or "thinking" in body,
            roles=[msg["role"] for msg in messages],
            content_sha256=[digest(msg["content"]) for msg in messages],
            system_prompt_sha256=digest(messages[0]["content"]),
            prompt_sha256=digest(messages[1]["content"]),
        )
        try:
            response = await transport(self, request)
        except Exception as exc:
            event("http_transport_error", error_type=type(exc).__name__)
            raise
        event("http_return", status=response.status_code)
        return response

    GuardedProviderTransport.handle_async_request = observed_http

    class ObservedReranker:
        def __init__(self, base, arm):
            self.base = base
            self.arm = arm
            self.model_name = base.model_name

        def rerank(self, query, candidates):
            if self.arm == "A":
                scores = self.base.rerank(query, candidates)
                details = [
                    {"chunk_id": c.chunk_id, "whole_score": s, "score": s}
                    for c, s in zip(candidates, scores, strict=True)
                ]
            else:
                from benchmarks.public_rag.evidence_sentence import SentenceEvidenceReranker

                result = SentenceEvidenceReranker(self.base).inspect(query, candidates)
                scores = tuple(item.score for item in result)
                details = [asdict(item) for item in result]
                if [item.chunk_id for item in result] != [c.chunk_id for c in candidates]:
                    raise ValueError("The candidate reranker changed the input identity")
            event("rerank_scores", arm=self.arm, input_scores=details)
            return scores

    async def for_request(request: Request):
        arm = request.headers["x-evidence-retention-arm"]
        if arm not in ("A", "B"):
            raise ValueError("The isolated API requires a frozen retrieval arm")
        components = await get_retrieval_components(request)
        return replace(components, reranker=ObservedReranker(components.reranker, arm))

    app.dependency_overrides[get_retrieval_components] = for_request

    # Rendering in the retrieval-only phase is measured by calling the production admission
    # and prompt functions on the returned full evidence, before playground's snippet truncation.
    # Only the playground uses this subclass. QA renders its own prompt directly in the service.
    from apps.api.routes import knowledge as playground

    class MeasuredPlayground(playground.HybridKnowledgeRetriever):
        async def retrieve_with_trace(self, context, query):
            result = await super().retrieve_with_trace(context, query)
            numbered = qa._select_evidence(
                result.evidence, max_chars=get_settings().knowledge_qa_max_evidence_chars
            )
            qa._evidence_prompt(query.text, numbered)
            return result

    playground.HybridKnowledgeRetriever = MeasuredPlayground


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--events", type=Path, required=True)
    args = parser.parse_args()
    import uvicorn

    from apps.api.main import app

    instrument(app, args.events)
    uvicorn.run(app, host="127.0.0.1", port=args.port, loop="none", access_log=False)


if __name__ == "__main__":
    main()
