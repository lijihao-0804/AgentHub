# How first answers are scored

Use the actual numbered excerpts, the question, the official annotation and
the response, without the treatment label, token count, latency or order.
Identical question/response/citation sets share one judgment. A judge records
one of the following and the unique citation numbers that support at least
one substantive claim made where they appear.

|Verdict|Rule|
|---|---|
|full|Answers the question, with every material claim supported by its attached evidence. A more complete or equivalent answer may be correct even when the extractive label is incomplete; explain the discrepancy.|
|partial|Provides a useful, supported part but explicitly leaves part unresolved. Do not credit a confident contradiction or an invented explanation as partial.|
|wrong|Wrong answer, wrong document, unsupported main conclusion or added material unsupported assertion. A citation can still support a separate limited claim.|
|refusal|HTTP 200 with insufficient evidence and no citations. Count as a correct refusal only on a predeclared unanswerable question; report questionable annotations explicitly.|
|failure|First HTTP response is an error. Counts as neither an answer nor a refusal.|

Citation precision is the count of individually supporting returned references
divided by all returned references. It does not measure coverage of every
claim, so a correct positive additionally requires the full-answer rule.
Original byte/locator checks are a separate metric. For a negative question
with a substantive positive answer, report the official target and what the
paper actually supports separately; keep the question and original label in
the frozen refusal denominator. Do not retroactively remove hard cases.

The retrieval verifier reports gold paper, verbatim gold paragraph and
verbatim annotated answer-span presence separately at candidate 20 and reader
6 (case-insensitive with no synonym expansion, on the target document).
True negatives have no evidence target, so they do not enter retrieval
denominators. A gold paragraph split across chunks may score a miss even when
one of those chunks is sufficient to answer; adjudicate the text itself.
