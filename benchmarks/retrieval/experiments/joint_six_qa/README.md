# Six-passage QA measurement

This runner measures the product's `POST /api/v1/knowledge/query` with the default BGE-M3 encoder, sparse tokenizer and BGE reranker. The baseline uses the main QA prompt and packing behavior at `4a0daa2`. The candidate enables `knowledge_qa_joint_six_v1`. Both arms use the same knowledge snapshot for each question. The observer records the 20-or-fewer hydrated candidates, the full rerank and dedup order, the numbered evidence rendered into the actual model request, the provider's first answer, and the final API result.

## Local inputs

Use the repository's Python environment and offline BGE weight snapshots at the revisions in `inputs.py`. The independent MuSiQue data freeze binds the 65 questions, their original twenty paragraphs and annotations. The private input root is `.scratch/joint-six-20261118`. The QA experiment writes only under `.scratch/joint-six-qa-20261118`. [Question registration](../../../public_rag/experiments/joint_six_20261118/manifest.json)

The measurement used PostgreSQL 16 bound to `127.0.0.1:56541`. Start the server from a fresh private copy of the cleanly stopped `.scratch/composite-qa-db/archive-copy` cluster. Leave the original files in place. Create an empty `agenthub_joint_six_fresh` database, run the project's Alembic migrations, then create `agenthub_joint_six_smoke` from that empty migrated database. Create the three historical databases from the following templates in the copied cluster.

| Group | Local database | Template with historical snapshots |
| --- | --- | --- |
| R | `agenthub_joint_six_r` | `agenthub_test_first_refusal_100` |
| B | `agenthub_joint_six_b` | `agenthub_test_bilingual_refusal_100` |
| W | `agenthub_joint_six_w` | `agenthub_nov18_rag` |

Fresh questions enter through the normal upload, worker ingestion and snapshot APIs. For R, B and W, the runner validates archived snapshot hashes, revisions, document identities and case bindings in the copied database, then copies the corresponding preflight Qdrant index. Both arms read that same index. Use one GPU process at a time. When measurements finish, stop the PostgreSQL process started on the new data directory.

## Network-free preparation

Run these module commands with the repository Python from the repository root. `prepare` builds local snapshots and indices. `preflight` exercises the normal gateway and both real QA validation attempts with a transport fixture. The smoke group has one baseline row. The formal preflight contains both arms for all 64 fresh and 214 protected questions. No test output from the fixture contributes to answer scores.

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
.venv/Scripts/python.exe -m benchmarks.retrieval.experiments.joint_six_qa.wire_check
.venv/Scripts/python.exe -m benchmarks.retrieval.experiments.joint_six_qa.runner prepare --groups smoke R B W
.venv/Scripts/python.exe -m benchmarks.retrieval.experiments.joint_six_qa.runner preflight --groups smoke
.venv/Scripts/python.exe -m benchmarks.retrieval.experiments.joint_six_qa.runner prepare --groups fresh
.venv/Scripts/python.exe -m benchmarks.retrieval.experiments.joint_six_qa.runner preflight --groups fresh R B W
```

## Registration and settlement

Each registration checks the committed application and instrumented runner, actual on-disk source and weight hashes, private data and bindings, model, authorized Beijing endpoint digest, serialized physical payload, question identities and peak-cost ceiling. `ledger freeze smoke` creates the first registration. `ledger freeze formal` requires a completed, reconciled smoke result. These actions create new files exclusively. Each paid batch uses its own exclusive directory and ledger.

The smoke stage permits one physical call. The formal stage permits 128 fresh QA calls and 214 candidate regressions, with at most one product correction per QA call. Before each physical call the meter validates the frozen parameters and input, reserves the actual UTF-8 request length plus framing allowance and all 256 permitted output tokens at the peak tariff, and durably writes the reservation. Transport, provider status, receipt identity, model or usage failures close the batch at that attempt. An open reservation is an unknown outcome. The original uncertain `2hop__75213_24779/baseline` remains permanently sealed.

The paid runner is invoked exclusively by the authorized operator with `dispatch --groups smoke` or `dispatch --groups fresh R B W` and the stage-specific `--freeze-sha`. Use read-only `ledger reconcile smoke` and `ledger reconcile formal` to recalculate settled and outstanding costs and inspect completion. The smoke receipt has zero cached tokens, so the archived November 18 smoke amount is the same under the formal tariff. For the fixed Beijing snapshot, a million input cache hits cost CNY 0.04 in peak hours and 0.02 off peak. Uncached input costs 2 or 1 and output costs 8 or 4. The provider receipt time determines the applicable rate. These are tariff estimates from reported usage. [Official snapshot pricing](https://help.aliyun.com/zh/model-studio/deepseek-v4-1-flash)

Run `python -m benchmarks.retrieval.experiments.joint_six_qa.report` against the completed private campaign to reproduce the public group counts. The report includes actual QA status, elapsed time, token usage, and the provider's completion details for API errors. The first cold call can include local model loading.
