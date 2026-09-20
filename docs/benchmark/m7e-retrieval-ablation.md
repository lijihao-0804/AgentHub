# M7-E Retrieval Strategy Ablation

Status: PASS — real host ablation executed.

- Dataset: `m3-retrieval-v1`
- Dataset hash: `8ff0df86a26822725b01540b493671a801a74d15eec07522bea8311f7817ef9a`
- Git commit: `76286b751ed7bc4dd397885c6ad65eb0951b6280`

| Strategy | DEV Final Recall@5 | HOLDOUT Final Recall@5 | ALL MRR@5 | ALL p95 (ms) |
|---|---:|---:|---:|---:|
| DENSE | 1.0000 | 1.0000 | 0.9333 | 61.845 |
| HYBRID | 0.9500 | 0.9000 | 0.8083 | 93.118 |
| HYBRID_RERANK | 1.0000 | 1.0000 | 0.9833 | 525.339 |
