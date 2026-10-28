# Gold answers and scoring

**Do not include this file in `summary_request.txt` or in a candidate summary.** Coordinates below refer to one-based lines in the full Apache Spark v3.5.9 upstream Markdown file identified in `README.md`; the local excerpt line is the upstream line minus 3936.

Score each item independently as 1 only when the answer preserves the stated fact and its material condition/negation. Otherwise score 0. Total: 0–10. A materially contradictory statement also makes the corresponding item 0.

## Task 1 reference answer

On failure or intentional shutdown, checkpointing and write-ahead logs let a query resume its previous progress and state. The checkpoint records the offset range processed in each trigger and running aggregates; its location must be on an HDFS-compatible file system and is configured on the `DataStreamWriter` when starting the query. Asynchronous progress tracking is supported only for stateless queries using the Kafka sink, and it does **not** support end-to-end exactly-once processing because a failure can change the offset ranges assigned to a batch. Turning it off may produce `IllegalStateException: batch x doesn't exist` or a missing offset-log message. The stated procedure is to re-enable `asyncProgressTrackingEnabled`, set `asyncProgressTrackingCheckpointIntervalMs` to `0`, run until at least two micro-batches have been processed, and then disable it; the guide says restart should then proceed normally.

## Task 2 reference answer

- Changing a rate limit such as `maxOffsetsPerTrigger` is allowed.
- Changing subscribed topics is generally not allowed; the guide warns results are unpredictable.
- File sink → Kafka sink is allowed; Kafka sees only new data.
- Kafka sink → file sink is not allowed.
- Changing a file sink's output directory is not allowed.
- Changing the Kafka output topic is allowed.
- Changing user-defined `ForeachWriter` code is allowed, but the semantics depend on that code.
- Changing a streaming aggregation's grouping key is not allowed across restart because the state schema must remain the same. Aggregation grouping-key or aggregate count/type changes are explicitly disallowed.

The guide defines “allowed” as a change whose effect may still lack well-defined semantics depending on the query and change. “Not allowed” means the restarted query is likely to fail with unpredictable errors. Sink-type changes are case-by-case; the two directions above have different outcomes.

## Ten scoring items with source evidence

| # | Required claim / scoring point | Source quote and coordinate |
|---|---|---|
| 1 | After failure or intentional shutdown, the previous query progress and state can be recovered and continued. | “you can recover the previous progress and state … and continue where it left off.” — `L3938` |
| 2 | Checkpoint records per-trigger offset ranges and running aggregates; location must be an HDFS-compatible path configured on the DataStreamWriter. | “range of offsets processed in each trigger” and “running aggregates”; “has to be a path in an HDFS compatible file system” — `L3938` |
| 3 | “Allowed” does not itself mean well-defined semantics; that depends on query/change. “Not allowed” may fail with unpredictable errors. | Definitions of both terms — `L3997–L4002` |
| 4 | Input source number/type changes are not allowed; rate-limit changes are allowed; subscribed topic changes are generally not allowed and unpredictable. | `L4006`, `L4011`, `L4013` |
| 5 | Sink-type changes are case-by-case; file→Kafka is allowed and Kafka sees only new data; Kafka→file is not allowed. | `L4015–L4020` |
| 6 | Sink-parameter semantics depend on sink/query; changing a file sink output directory is not allowed; changing the Kafka output topic is allowed; changing ForeachWriter code is allowed but its effect depends on the code. | `L4024–L4031` |
| 7 | Stateful operation state is checkpointed/restored, but state schema must remain the same; aggregation grouping key/count/type changes are not allowed. | `L4041–L4049` |
| 8 | Asynchronous progress tracking supports only stateless queries with Kafka sink. | `L4098–L4101` |
| 9 | Async progress tracking does not support end-to-end exactly-once because failure can change batch offset ranges. | `L4101–L4102` |
| 10 | Safe disable sequence: re-enable tracking, set checkpoint interval to 0, process at least two micro-batches, then disable; preserve the possible missing-batch symptom as a symptom, not the fix. | Error and suggested recovery — `L4104–L4117` |

## Scoring record template

| Task | Item | 0/1 | Answer evidence (quote or span) | Source coordinate verified? | Note |
|---|---:|---:|---|---|---|
