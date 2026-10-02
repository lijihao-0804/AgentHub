# Support DEV policy retrieval

DEV-only: 24 queries, 8 fictional policies. No HOLDOUT exposure.
This measures policy retrieval; it does not measure answer quality.

| Strategy | Candidate Recall@20 | Final Recall@5 | MRR@5 | p95 ms |
| --- | ---: | ---: | ---: | ---: |
| DENSE | 1.0000 | 1.0000 | 1.0000 | 85.006 |
| HYBRID_RERANK | 1.0000 | 1.0000 | 0.9792 | 995.675 |

HOLDOUT: NOT_AVAILABLE (0 samples).

Dataset hash: `f1bb7d83dda8648bbb927813aaf562c27fcd8151e289083855ff448175ae32c0`

Collection: `agenthub_mi34_41b02e59e1e14e73876b8cbee0d08244`
