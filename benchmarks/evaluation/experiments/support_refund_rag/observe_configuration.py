"""Save a secret-free local configuration projection; no service/model calls."""

from __future__ import annotations

import json
from dataclasses import MISSING, fields
from pathlib import Path

from packages.agent_runtime.runtime_config import DEFAULT_RUNTIME_LIMITS, derive_context_budget
from packages.core.config.settings import Settings
from packages.knowledge.contracts import RetrievalQuery

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[3]


def main() -> None:
    route = json.loads((REPO / "benchmarks/public_rag/model-route.json").read_text("utf-8"))
    primary = route["primary"]
    settings = Settings()
    observed = {
        "status": "OBSERVED_LOCAL_CONFIGURATION_NOT_PUBLISHED_RUNTIME",
        "model_source": "benchmarks/public_rag/model-route.json primary",
        "primary": {
            key: primary[key]
            for key in (
                "provider",
                "model",
                "temperature",
                "max_tokens",
                "timeout_seconds",
                "capabilities",
            )
        },
        "configured_fallback_count": len(route["fallbacks"]),
        "experiment_required_fallbacks": [],
        "runtime_source": "packages.agent_runtime.runtime_config server-owned defaults",
        "runtime_defaults": DEFAULT_RUNTIME_LIMITS,
        "derived_context_budget": derive_context_budget(
            primary["capabilities"]["max_context_tokens"]
        ),
        "retrieval_defaults_source": "packages.knowledge.contracts.RetrievalQuery",
        "retrieval_defaults": {
            field.name: field.default
            for field in fields(RetrievalQuery)
            if field.default is not MISSING
        },
        "settings_allowlist": {
            key: getattr(settings, key)
            for key in (
                "knowledge_embedding_model",
                "knowledge_embedding_device",
                "knowledge_reranker_model",
                "knowledge_reranker_device",
                "knowledge_dense_vector_size",
                "knowledge_rrf_k",
                "knowledge_min_rerank_score",
                "knowledge_superseded_rank_penalty",
                "knowledge_chunk_size_chars",
                "knowledge_chunk_overlap_chars",
            )
        },
        "pricing": "UNKNOWN; route config contains no verified billing tariff",
        "publication_gate": (
            "Before S1 acceptance, select a specific valid published config and compare its "
            "actual frozen output/temperature/runtime/retrieval values. Observed defaults "
            "are not evidence of a published version. Freeze all differences before dispatch."
        ),
    }
    (ROOT / "configuration-observed.json").write_text(
        json.dumps(observed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Saved secret-free local configuration; no model or database calls.")


if __name__ == "__main__":
    main()
