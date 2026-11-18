# FiQA source recall through evidence selection

Use the archived 648 BEIR FiQA test queries and the two 30-chunk production retrieval lanes. These labels have been used by the project before. Compare three fixed policies without using the labels for selection.

1. Fuse with the production reciprocal rank function (`k=60`), re-rank the top 20 with the local BGE re-ranker and pass the first six to the production evidence selector.
2. Increase only the re-ranking pool to the first 40 unique fused chunks, or the entire union if smaller. Pass six in re-ranked order.
3. At the same expanded depth, preserve the first five chunks of the baseline and fill the sixth slot with the highest re-ranked remaining chunk. Keep the same six-slot limit and budgets.

Report macro document recall and the number of questions with a relevant source for candidate, top six and serialized evidence. Report losses/gains for both macro recall and per-query positive-source sets. Evaluate the application setting (16,000 estimated evidence tokens) and a fixed 512-token stress test separately. Check the original top 20 chunk IDs against the archive. Use the same single frozen BGE checkpoint, deterministic fusion identity and tie break. Score in batches of eight and report the number of pairs scored. Score comparisons use the same logits for the same query/chunk so differences isolate the policy. No fee-based requests. FiQA labels designate relevant documents and do not designate an answer sentence; separately report the bilingual QA study's gold-span presence in actual model messages.

This study uses existing full-corpus production lane rankings. Verify the input files, corpus snapshot and chunk identity before scoring. Do not bundle original query text, source documents or database identifiers in the published derivatives. A recall increase with a decrease for previously successful questions is grounds to withhold a retrieval default change.
