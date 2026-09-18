# M3 Retrieval Evaluation Baseline

Status: PASS — real host baseline executed.

- Dataset: `m3-retrieval-v1`
- Dataset hash: `8ff0df86a26822725b01540b493671a801a74d15eec07522bea8311f7817ef9a`
- Git commit: `1bff1fa2f843fae3b13fad9aeddba4cedc261b88`
- Snapshot: `9f672935-1984-4bfd-b78f-e3a06e46d293`
- Snapshot hash: `5caf6b4d51aed255ae08076dd326a1ecd08fff59fe4aed9e8f3f149828ac4dbc`
- Embedding model: `BAAI/bge-m3`
- Reranker model: `BAAI/bge-reranker-v2-m3`
- Device: embedding=cuda, reranker=cuda, torch=2.14.0+cu130, CUDA=True, GPU=NVIDIA GeForce RTX 3060 Laptop GPU

## Metrics

| Split | Candidate Recall@20 | Final Recall@5 | MRR@5 |
|---|---:|---:|---:|
| dev | 1.0000 | 1.0000 | 0.9750 |
| holdout | 1.0000 | 1.0000 | 1.0000 |
| overall | 1.0000 | 1.0000 | 0.9833 |

## Failure analysis

- dev: 1 failures
- holdout: 0 failures
- overall: 1 failures

This baseline is retrieval-only. It does not measure answer quality or citation QA.
M3 overall acceptance is recorded only after this real baseline and all prior milestone checks pass.
