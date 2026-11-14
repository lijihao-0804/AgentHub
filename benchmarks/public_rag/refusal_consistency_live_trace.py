"""Measure two frozen Citation QA implementations in one loopback public-QASPER API."""

import argparse
import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path

from fastapi import Request

from benchmarks.public_rag.evidence_retention_trace import instrument as observe_retrieval


def source_hash(path):
    return hashlib.sha256(Path(path).read_text(encoding="utf-8").encode()).hexdigest()


def load_candidate(path):
    # The old service is the normal import in the unmodified baseline tree. A separate
    # module keeps the candidate's functions and call sites independent of baseline.
    name = "packages.knowledge._public_qasper_candidate"
    if name in sys.modules:
        raise RuntimeError("Refusal comparison was already installed in this process")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def instrument(app, path, candidate_path, freeze):
    from apps.api.knowledge_dependencies import get_retrieval_components
    from apps.api.routes import citation_qa as route
    from packages.core.canonical.json_parse import load_unique_json
    from packages.knowledge import citation_qa as baseline
    from packages.knowledge import refusal_consistency as rule
    from packages.model_gateway.adapters.litellm.adapter import LiteLLMProviderAdapter

    if path.exists() or app.dependency_overrides.get(get_retrieval_components) is not None:
        raise RuntimeError("Expected a new trace file and a fresh API process")
    sources = {"A": source_hash(baseline.__file__), "B": source_hash(candidate_path)}
    if (
        sources != freeze["qa_source_sha256_lf"]
        or source_hash(rule.__file__) != freeze["rule_sha256_lf"]
        or source_hash(__file__)
        != freeze["source_sha256_lf"]["benchmarks/public_rag/refusal_consistency_live_trace.py"]
    ):
        raise RuntimeError("Frozen QA or measurement code differs")
    candidate = load_candidate(candidate_path)
    if candidate._PROMPT_JSON_SYSTEM != baseline._PROMPT_JSON_SYSTEM or inspect.getsource(
        candidate._evidence_prompt
    ) != inspect.getsource(baseline._evidence_prompt):
        raise RuntimeError("The refusal comparison requires identical reader prompts")
    observe_retrieval(app, path)
    # The tracer records the exact user message for both classes, using the same
    # production rendering function. The shared retrieval class is already instrumented.
    candidate._evidence_prompt = baseline._evidence_prompt
    retrieval = app.dependency_overrides[get_retrieval_components]

    def event(kind, **fields):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, **fields}, ensure_ascii=False) + "\n")

    async def components_for_request(request: Request):
        arm = request.headers["x-refusal-consistency-arm"]
        if arm not in sources or request.headers.get("x-evidence-retention-arm") != "B":
            raise ValueError("Both frozen QA arms require the same evidence-sentence reranker")
        components = await retrieval(request)
        # The prior tracer returns a new wrapper for each request, around the shared
        # GPU weights. Only this request's wrapper is annotated or used to route QA.
        components.reranker.qa_arm = arm
        return components

    def service_for_request(**kwargs):
        reranker = kwargs["retrieval_components"].reranker
        arm = reranker.qa_arm
        if reranker.arm != "B":
            raise ValueError("Incorrect retrieval arm")
        event(
            "qa_variant",
            arm=arm,
            qa_source_sha256_lf=sources[arm],
            verify_refusal_consistency=arm == "B",
        )
        if arm == "B":
            return candidate.CitationQaService(**kwargs, verify_refusal_consistency=True)
        return baseline.CitationQaService(**kwargs)

    app.dependency_overrides[get_retrieval_components] = components_for_request
    route.CitationQaService = service_for_request
    complete = LiteLLMProviderAdapter.complete

    async def observe_output(self, *args, **kwargs):
        result = await complete(self, *args, **kwargs)
        output = None
        try:
            parsed = load_unique_json(result.content)
        except (ValueError, TypeError, RecursionError):
            status = "invalid_json"
        else:
            try:
                output = baseline._CitationQaModelOutput.model_validate(
                    parsed, strict=True
                ).model_dump()
            except (ValueError, TypeError):
                status = "invalid_schema"
            else:
                status = "schema_valid"
        # Only this authenticated benchmark's public questions are sent here. Persist
        # the three schema fields; drop provider metadata, hidden reasoning and headers.
        event(
            "model_output",
            json_status=status,
            output=output,
            content_sha256=hashlib.sha256(result.content.encode()).hexdigest(),
        )
        return result

    LiteLLMProviderAdapter.complete = observe_output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--events", required=True, type=Path)
    p.add_argument("--candidate", required=True, type=Path)
    p.add_argument("--freeze", required=True, type=Path)
    p.add_argument("--port", required=True, type=int)
    args = p.parse_args()
    import uvicorn

    from apps.api.main import app

    instrument(app, args.events, args.candidate, json.loads(args.freeze.read_bytes()))
    uvicorn.run(app, host="127.0.0.1", port=args.port, loop="none", access_log=False)


if __name__ == "__main__":
    main()
