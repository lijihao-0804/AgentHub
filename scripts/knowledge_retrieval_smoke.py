"""Manual real BGE/Qdrant smoke; this is not a benchmark and never runs in CI."""

from __future__ import annotations

import argparse
import time

from packages.core.config.settings import get_settings
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.contracts import RerankCandidate, VectorRecord, VectorScope
from packages.knowledge.point_ids import deterministic_point_id
from packages.knowledge.retrieval import fuse_reciprocal_rank


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="中文 knowledge retrieval")
    parser.add_argument("--collection", default="agenthub_knowledge_smoke")
    args = parser.parse_args()
    settings = get_settings()
    dense = BgeM3DenseEmbedder(
        model_name=settings.knowledge_embedding_model,
        device=settings.knowledge_embedding_device,
        expected_dimension=settings.knowledge_dense_vector_size,
        batch_size=2,
    )
    sparse = BgeM3SparseEncoder(model_name=settings.knowledge_embedding_model)
    reranker = BgeReranker(
        model_name=settings.knowledge_reranker_model,
        device=settings.knowledge_reranker_device,
        batch_size=2,
    )
    index = QdrantVectorIndex(
        url=settings.qdrant_url,
        collection_name=args.collection,
        dense_vector_size=settings.knowledge_dense_vector_size,
        timeout_seconds=settings.knowledge_qdrant_timeout_seconds,
    )
    corpus = (
        ("smoke-zh", "企业知识库支持中文检索和权限边界。"),
        ("smoke-en", "The knowledge hub supports English retrieval and tenant scope."),
        ("smoke-noise", "A deliberately unrelated chunk about garden tools."),
    )
    started = time.perf_counter()
    index.ensure_collection()
    documents = dense.embed_documents(tuple(text for _, text in corpus))
    sparse_vectors = sparse.encode_documents(tuple(text for _, text in corpus))
    index.upsert(
        tuple(
            VectorRecord(
                point_id=deterministic_point_id(chunk_id),
                chunk_id=chunk_id,
                dense=documents[index_],
                sparse=sparse_vectors[index_],
                payload={
                    "workspace_id": "smoke-workspace",
                    "knowledge_base_id": "smoke-kb",
                    "document_id": f"document-{index_}",
                    "document_revision_id": "smoke-revision",
                    "chunk_id": chunk_id,
                    "locator": {"type": "smoke"},
                },
            )
            for index_, (chunk_id, _text) in enumerate(corpus)
        )
    )
    scope = VectorScope("smoke-workspace", "smoke-kb", ("smoke-revision",))
    dense_hits = index.dense_search(dense.embed_query(args.query), scope=scope, limit=30)
    sparse_hits = index.sparse_search(sparse.encode_query(args.query), scope=scope, limit=30)
    fused = fuse_reciprocal_rank(dense_hits, sparse_hits, rrf_k=settings.knowledge_rrf_k)
    text_by_chunk = dict(corpus)
    rerank_candidates = tuple(
        RerankCandidate(item.chunk_id, text_by_chunk[item.chunk_id]) for item in fused
    )
    rerank_scores = reranker.rerank(args.query, rerank_candidates)
    ranked = sorted(
        zip(rerank_candidates, rerank_scores, strict=True),
        key=lambda item: (-item[1], item[0].chunk_id),
    )[:6]
    print(
        {
            "query": args.query,
            "rank": [candidate.chunk_id for candidate, _score in ranked],
            "rerank_scores": [score for _candidate, score in ranked],
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    )


if __name__ == "__main__":
    main()
