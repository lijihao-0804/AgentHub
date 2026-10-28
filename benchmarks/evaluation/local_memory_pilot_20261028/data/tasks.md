# 固定后续任务

以下两题只依赖 `source_document.md`。它们是待回答的问题，不包含参考答案。

## Task 1 — Checkpoint recovery and turning off asynchronous progress tracking

A stateless Structured Streaming query with a Kafka sink uses asynchronous progress tracking. After a failure, the operator wants to turn the setting off and sees `IllegalStateException: batch x doesn't exist` while restarting. Based only on the supplied guide:

1. Explain what ordinary checkpoint recovery saves and restores, the checkpoint storage-path constraint, and where that location is configured when starting the query.
2. State whether asynchronous progress tracking supports end-to-end exactly-once processing and why.
3. Give the guide's sequence for safely turning the setting off, including the required checkpoint-interval value and minimum number of micro-batches.

Separate the error symptom from the recommended recovery sequence.

## Task 2 — Classifying changes across a restart from the same checkpoint

A team will restart a streaming query from the same checkpoint and proposes these changes:

1. Change the input rate limit (`maxOffsetsPerTrigger`).
2. Change the subscribed Kafka topic.
3. Change a file sink to a Kafka sink.
4. Change a Kafka sink to a file sink.
5. Change the output directory of a file sink.
6. Change the output topic of a Kafka sink.
7. Change the code of a user-defined `ForeachWriter` sink.
8. Change the grouping key of a streaming aggregation.

For each proposal, classify it using the guide's terms: allowed, not allowed, or conditional/semantics not well-defined. Preserve any direction-specific effect or caveat given in the guide. Explain the state-schema constraint relevant to the aggregation change; do not generalize beyond the guide's examples.
