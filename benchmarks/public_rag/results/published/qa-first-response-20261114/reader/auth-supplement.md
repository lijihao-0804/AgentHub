# Four questions stopped before retrieval

The frozen reserve16 first-response table is immutable. For the final four
questions in its published order, all three arms returned HTTP 401 with
`ACCESS_TOKEN_EXPIRED` and no trace events. These 12 attempts are failures
in the original denominator of 48. They establish neither reader behavior nor
the presence or absence of a retrievable answer. The evaluated client signed
in once per service at scope startup; its local access-token lifetime was 900
seconds. The first rejected request was more than 15 minutes after the first
question started.

Before collecting any model response for these four questions, the follow-up
was restricted to their original arm order, existing snapshot, profiles, source
trees and protocol. Obtain valid local authentication before the question.
Save one new HTTP response per arm and question to a separate directory; retain
any 502 and do not replay a request that reached the model. Compare the
three ordered candidate, reranker, visible evidence and wire-input digests,
and verify returned quotations against the original corpus.

Report this as a diagnostic supplement, with separate semantic review. It may
clarify the authenticated reader result but cannot replace any original 401 or
qualify a strategy for promotion.

All 12 authenticated HTTP requests returned 200. Each arm fully answered
three of four answerable questions (WikiHop, METEOR, HuffPost) and refused
the same MH17 question about proposed improvements. The combined 13 returned
citations match the original public text and support at least one attached
claim apiece. All arms retrieved the four target papers. Only one complete
annotated paragraph was present among the top 20, and only one among the six
reader blocks. This is consistent with correct short answers being supported
by another passage while an exact annotated paragraph is absent.

[Frozen follow-up](authenticated-supplement/protocol.json) ·
[Separate judgments and counts](authenticated-supplement/review.json) ·
[Corpus, source, scope and model-input verification](authenticated-supplement/verification.json).

To reproduce the supplement, set `$s` to the `reader/authenticated-supplement`
directory. Each command reads saved HTTP receipts. The optional packet and
report outputs are separate from the original three-arm decision.

```powershell
.venv/Scripts/python -m benchmarks.public_rag.qa_first_response_reader_review $s --supplement --packet .scratch/blind-reader-supplement.json
.venv/Scripts/python -m benchmarks.public_rag.qa_first_response_reader_review $s --supplement --judgments "$s/judgments.json" --report "$s/review.json"
.venv/Scripts/python -m benchmarks.public_rag.qa_first_response_reader_review $s --supplement --qasper data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json --scifact data/external/rag-public-20261021/scifact/extracted/scifact/corpus.jsonl --verification "$s/verification.json"
.venv/Scripts/python -I -S benchmarks/public_rag/results/published/qa-first-response-20261114/verify.py
```
