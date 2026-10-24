# Batched Publication Dependency Review

## Scope and method

This is a read-only source and test-input audit of the proposed publication batches. It covers explicit imports and file reads needed for atomic batch publication. It does not evaluate RAG quality, alter scoring, inspect private `.env` data, run tests, or make Git, network, or API changes.

The repository follows `AGENTS.md`; `.env.example` contains placeholders only. Raw benchmark outputs under `benchmarks/public_rag/results/*` are ignored and remain local-only except for `results/published/**`.

## Boundary verdicts

### BATCH1 to BATCH2

**No production source import violates the boundary.** No Python module in `packages/knowledge`, `packages/agent_runtime`, `packages/model_gateway`, `apps/api`, `apps/worker`, or `scripts/model_route.py` imports code under `benchmarks/`.

There is one file-data dependency:

- `scripts/model_route.py:45` defaults to `benchmarks/public_rag/model-route.json`.
- `.env.example` documents that benchmark path.
- `tests/unit/test_configured_model_route.py:34` reads the JSON directly.
- `docs/deployment/local-model-route.md` and `benchmarks/public_rag/README.md` refer to the same path.

**Required exception:** publish `benchmarks/public_rag/model-route.json` and `tests/unit/test_configured_model_route.py` with BATCH1, or publish the route configuration and its test as an explicit prerequisite component before BATCH2. Without that exception, BATCH1's documented default and route test are incomplete.

### BATCH2 to BATCH3

**No BATCH2 production or benchmark module imports a BATCH3 module.** Existing `benchmarks/public_rag/experimental/attributed_reader.py` belongs in BATCH2.

BATCH3's `benchmarks/public_rag/experimental/entity_delivery.py:15` imports the BATCH2 module `experimental.attributed_reader`. BATCH2 also contains modules that reference `experimental.attributed_reader`: `attributed_reader_runner.py`, `prompt_trial.py`, `prompt_trial_report.py`, `support_dataset.py`, and `support_dataset_runner.py`. Keep the attributed-reader module in BATCH2 and publish BATCH2 before BATCH3.

BATCH2 benchmark code depends on BATCH1 production packages. In particular, `benchmarks/public_rag/scifact_runner.py:21` imports BATCH1's `apps.worker.tasks.knowledge._vector_records`. This is a sequencing dependency, not a broken reverse import.

### BATCH4

`docs/research/` does not exist, so the proposed BATCH4 membership cannot be verified. The parent must name the exact "research summaries" files before publication. Possible material exists under `docs/reviews/`, `docs/benchmark/`, and `docs/deployment/`, but none of it should be inferred as BATCH4 solely from its topic.

## Tests requiring local-only raw results

The following tests should receive an explicit local-input gate or `skipif` when their raw artifacts are absent. Where an equivalent checked-in aggregate exists, the test can be repointed to it. These changes should preserve scoring and production behavior.

| Batch | Test | Local-only input | Publication handling |
|---|---|---|---|
| BATCH2 | `tests/unit/test_public_rag_consumer_report.py` | `results/consumer-rag-20261023/protocol.json` | Gate on the raw input, or use the checked-in `results/published/consumer-rag-20261023/protocol.json` if it satisfies the test contract. |
| BATCH2 | `tests/unit/test_public_rag_learned_runner.py` | `results/learned-sparse-20261022/protocol.json` | Gate on the raw input, or use the checked-in `results/published/learned-sparse-20261022/protocol.json` if it satisfies the test contract. |
| BATCH2 | `tests/unit/test_public_rag_fiqa_confirmation.py` | Raw `results/onyx-selection-off-20261022/frozen-selection.json` and `results/fiqa-confirmation-20261022/method-protocol.json`; the exact-recipe test also reaches raw `measured-source.zip`. | Gate all affected tests unless compact, non-secret fixtures are published. No published equivalents were found for the two JSON inputs. |
| BATCH2 | `tests/unit/test_attributed_reader_report.py` | `results/attributed-reader-20261023/protocol.json` | Gate on the raw input, or use the checked-in `results/published/attributed-reader-20261023/protocol.json` if it satisfies the test contract. |
| BATCH3 | `tests/unit/test_answer_path_data.py` | `results/paragraph-qa-20261023/frozen-dataset.json` and `results/paragraph-qa-20261023/live-run-2/run.json`; it also writes `results/answer-path-20261024/candidate-dataset.json` under the ignored results tree. | Gate tests that need the absent raw inputs. A dedicated temporary output location may be used if required by the test contract. |
| BATCH3 | `tests/unit/test_entity_delivery_data.py` | No raw result input. It creates an ignored `tests/unit/.entity-delivery-fixture/` temporary directory. | No raw-result gate needed. |

## Publication order

1. BATCH1 plus the explicit model-route configuration/test exception, or a separately published route-configuration prerequisite.
2. BATCH2, including existing `experimental/attributed_reader.py` and the raw-input gates above.
3. BATCH3 after BATCH2; it depends on `experimental.attributed_reader`.
4. BATCH4 after the parent identifies the exact research-summary paths.
