"""Manual real-model smoke; never run this from CI."""

from __future__ import annotations

import math

from packages.core.config.settings import get_settings
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.contracts import RerankCandidate


def main() -> None:
    settings = get_settings()
    dense = BgeM3DenseEmbedder(
        model_name=settings.knowledge_embedding_model,
        device=settings.knowledge_embedding_device,
        batch_size=2,
        expected_dimension=settings.knowledge_dense_vector_size,
    )
    reranker = BgeReranker(
        model_name=settings.knowledge_reranker_model,
        device=settings.knowledge_reranker_device,
        batch_size=2,
    )
    texts = ("这是一个中文知识库片段。", "This is an English knowledge chunk.")
    vectors = dense.embed_documents(texts)
    if any(len(vector) != dense.dimension for vector in vectors):
        raise RuntimeError("dense vector dimension mismatch")
    if not all(math.isfinite(value) for vector in vectors for value in vector):
        raise RuntimeError("dense vector contains a non-finite value")
    scores = reranker.rerank(
        "knowledge chunk",
        tuple(RerankCandidate(f"model-smoke-{index}", text) for index, text in enumerate(texts)),
    )
    if len(scores) != len(texts) or not all(math.isfinite(score) for score in scores):
        raise RuntimeError("reranker returned invalid scores")
    print(
        {
            "dense_dimension": dense.dimension,
            "document_count": len(vectors),
            "rerank_scores": scores,
        }
    )


if __name__ == "__main__":
    main()
