# Citation QA: first public response and supporting evidence

Decision rules written before the new live responses. Baseline: AgentHub
`f8e7878d72471a778b238ee0ff1fbefb0e80c18f`. The intervention under test
is non-thinking generation on the already allowlisted Beijing DeepSeek
`deepseek-v4.1-flash-1017` route for `PROMPT_JSON`. Both arms must use the
same temperature, model profile, output cap, retrieval configuration and
immutable knowledge snapshot for each question. Keep the same initial
question, evidence and output/citation requirements. No synthesis of answers
from evaluation labels. No extra HTTP attempts on failed questions.

* Separate independently selected public TRAIN questions from the historical
  failures and preservation controls. Freeze the IDs, categories and selection
  procedure before generation. The new set is a development confirmation, not
  a formal AgentHub holdout. Only public benchmark documents may go to the
  configured model. Include genuine unanswerable questions.
* Count the first HTTP response including all errors. Report first HTTP 200
  separately from the first correct and supported answer (answerable
  denominator), appropriate refusal (unanswerable denominator), false refusals
  and erroneous assertions. Partial answers are reported separately; they do
  not count as fully correct. An upstream failure is never a correct refusal.
  Distinguish the number of outbound model calls from the number of HTTP
  requests and distinguish structural validity from semantic correctness.
* Review the substantive answer against the reference and what the numbered
  original excerpts actually say. Treat extraneous claims, wrong numbers,
  document confusion or a citation that merely mentions the subject as
  defects. Verify original quote provenance independently; record both quote
  integrity and semantic support. Deduplicate identical masked pairs before
  adjudication. Report the paired gains and losses; aggregate scores must not
  conceal a previously correct answer that was lost.
* Check upstream retrieval independently of HTTP 200 where available:
  same ranked sources/chunks in the top 20 candidates and in the six final
  results. For positive cases report gold source and answer-bearing evidence
  reach, with the actual match rule and denominators. A shared passage does
  not imply a correct answer. For the two reported anomalies, diagnose the
  historic event separately from any new behavior.
* Promote a behavioral change only if first-response reliability improves
  on the independent paired questions, with no loss of previously fully
  supported answers or correct refusals, no new unsupported positive answers
  or worse semantic citation precision, and all returned citations resolve to
  the right original text. Require identical retrieval or explain any impact;
  no retrieval gain is expected. Report cost/usage/latency with missing data
  explicit. Historical symptoms alone are insufficient for promotion. Failure
  of a gate leaves the treatment experimental. Any new treatment after
  inspecting results requires another, untouched cohort before promotion.
* Historical and current diagnostics may retain bounded structural enums,
  token counts, safe finish categories, request correlations and timestamps,
  never raw provider errors, reasoning, credentials or private prompts.
