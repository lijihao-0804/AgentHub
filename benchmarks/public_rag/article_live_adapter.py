"""Install a benchmark-only candidate hook in the real authorized retrieval path."""

import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.sparse import load_npz

from benchmarks.public_rag.article_fusion_candidates import select_local_passages
from benchmarks.public_rag.article_parent_methods import select_parent_chunks
from benchmarks.public_rag.scifact_recall_forensics import (
    fuse,
    idf_weights,
    normalized_vectors,
    ranking,
    sparse_scores,
)
from packages.knowledge import retrieval


def install(directory: Path, scope_name: str, arm: str):
    scope = json.loads((directory / f"{scope_name}-assets.json").read_bytes())
    rows = scope.pop("chunks")
    chunks = defaultdict(list)
    for chunk_id, row in rows.items():
        chunks[row["source_id"]].append(
            {"document_id": row["source_id"], "chunk_id": chunk_id, "text": row["text"]}
        )
    revisions = {r["revision"] for r in rows.values()}
    original = retrieval.fuse_reciprocal_rank
    if arm in ("full_parent4", "title_standalone"):
        ids = np.asarray(scope["parent_ids"])
        prefix = "title" if arm == "title_standalone" else "parent"
        matrix = normalized_vectors(np.load(scope[prefix + "_dense"]))
        lexical = load_npz(scope[prefix + "_sparse"]).tocsc()
        idf = idf_weights(lexical)
        vectors = normalized_vectors(np.load(scope["chunk_dense"]))
        groups = defaultdict(list)
        for i, c in enumerate(scope["chunk_order"]):
            groups[rows[c]["source_id"]].append((i, c))

    def select(
        query,
        vector_scope,
        dense_query,
        sparse_query,
        dense_hits,
        sparse_hits,
        *,
        rrf_k,
        candidate_top_k,
    ):
        if (
            query.knowledge_snapshot_id != scope["snapshot"]
            or vector_scope.workspace_id != scope["workspace"]
            or vector_scope.knowledge_base_id != scope["kb"]
            or set(vector_scope.document_revision_ids) != revisions
        ):
            raise ValueError("benchmark asset is outside the authorized snapshot")

        def lane(hits):
            return [
                {
                    "chunk_id": h.payload["chunk_id"],
                    "document_id": rows[h.payload["chunk_id"]]["source_id"],
                    "score": h.score,
                }
                for h in hits
            ]

        if arm == "article_local_passage":
            result = select_local_passages(
                query.text, lane(dense_hits), lane(sparse_hits), chunks, limit=candidate_top_k
            )
        elif arm in ("full_parent4", "title_standalone"):
            base = [
                {
                    "chunk_id": h.chunk_id,
                    "document_id": rows[h.chunk_id]["source_id"],
                    "score": h.retrieval_score,
                }
                for h in original(
                    dense_hits, sparse_hits, rrf_k=rrf_k, candidate_top_k=candidate_top_k
                )
            ]
            qv = np.asarray(dense_query, dtype=np.float32)
            dense = ids[ranking(matrix @ qv, ids)].tolist()
            sparse = ids[
                ranking(sparse_scores(lexical, idf, sparse_query), ids, positive=True)
            ].tolist()
            parents = fuse(dense, sparse)[:20]
            best = {}
            for parent in parents:
                group = groups[parent]
                scores = vectors[[i for i, _ in group]] @ qv
                best[parent] = min(
                    zip(scores, [c for _, c in group], strict=True),
                    key=lambda pair: (-pair[0], pair[1]),
                )[1]
            if arm == "title_standalone":
                result = [
                    {
                        "chunk_id": best[p],
                        "document_id": p,
                        "score": sum(
                            1 / (60 + lane.index(p) + 1)
                            for lane in (dense[:30], sparse[:30])
                            if p in lane
                        ),
                    }
                    for p in parents
                ]
                (directory / "title_standalone_lanes.json").write_text(
                    json.dumps({"dense": dense[:30], "sparse": sparse[:30], "fused": parents}),
                    encoding="utf8",
                )
            else:
                result = select_parent_chunks(base, parents, best)
        else:
            raise ValueError(arm)
        return tuple(
            retrieval._FusedCandidate(h["chunk_id"], h["score"], i)
            for i, h in enumerate(result, start=1)
        )

    # Only this isolated benchmark API process installs the alternative candidate pool.
    def fuse_candidates(self, query, scope, dense_query, sparse_query, dense_hits, sparse_hits):
        return select(
            query,
            scope,
            dense_query,
            sparse_query,
            dense_hits,
            sparse_hits,
            rrf_k=self.rrf_k,
            candidate_top_k=query.candidate_top_k,
        )

    if not hasattr(retrieval.HybridKnowledgeRetriever, "_fuse_candidates"):
        raise RuntimeError("missing experimental candidate hook")
    retrieval.HybridKnowledgeRetriever._fuse_candidates = fuse_candidates
