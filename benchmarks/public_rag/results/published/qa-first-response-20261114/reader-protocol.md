# Final reader confirmation, frozen before dispatch

On the first budget comparison, enlarging the output allowance alone did
not retain every supported answer. Test one new reader prompt addressing the
specific failure class: answer only what is asked, require every material
qualifier/number/source to occur in the cited passage, explicitly abstain if
the requested quantity, comparison or complete list is absent. No
question-specific rules, local answer rewriting, automatic extra sends,
invented citations or changes to retrieval.

Third dataset: 16 questions in 16 additional QASPER TRAIN papers; 10
answerable and 6 official negatives. Exclude historical exposures and
reservations and both earlier cohorts by paper and question. Reserve file
SHA-256: `beae2b767d37ce51bcd9d753068a6b8772d1aa8d2f97a097c582329b02e560ad`.
The four historical diagnostics (Italian embeddings, drunk-text hashtags,
Visual Genome data counts and SciFact 620) are separate from the denominator.

Compare three public HTTP first responses per question: original reader at
512 (A), original reader at the configured 2,048 (B), proposed reader at
2,048 (C). Same snapshot, temperature, timeout, provider-default thinking and
citation validator; no regeneration to overwrite failures. Freeze prompt text,
source, input cap and ordering before generation. Audit actual wire evidence,
model calls, output budgets and finish categories. An HTTP 200 with purportedly
supported but incorrect facts cannot count as a correct answer.

Primary user-facing decision is C versus the legacy QA setup A. Require
strictly more successful HTTP first responses, no lost old fully supported
answer or correct refusal, no new incorrect positive, non-decreasing citation
precision and verified original citations. For C versus the actually configured
B, require no loss in first-response count, correct answers/refusals, or
citation accuracy. Require an observed benefit on at least one reliability or
semantic measure over B before promoting its new prompt. Audit historical
controls separately, especially the unsupported added dataset source. Use the
same blinded rubric; resolve reviewer disagreements from the cited text before
unmasking. A failure leaves the relevant intervention off the normal path.
