# Blind first-response judgment

Read the question, the actual answer, each numbered excerpt and its paper ID, and the official reference. Judge each masked item once. The two reviewers record decisions independently. The adjudicator reviews every disagreement on verdict, supporting citation numbers, or whole-answer support while arm identities remain hidden. Identical contents share one judgment, including reconstructed responses when available.

| Judgment | Rule |
| --- | --- |
| `full` | Answers the question at the requested level of specificity and grounds every material claim in its attached cited text. Accept equivalent wording and source-supported corrections to an incomplete annotation. |
| `partial` | Supplies a useful, grounded part of the requested answer and leaves an identifiable part unresolved. A true contextual fact alone earns no answer credit when it does not answer any requested part. |
| `wrong` | Gives an incorrect or unsupported substantive answer, adds a material unsupported assertion, or presents surrounding facts as a positive response to a question they do not answer. |
| `refusal` | HTTP 200, `insufficient=true`, no citations. Counts as correct only on an official negative. |
| `failure` | The first HTTP response was an error. Keep the question in its original denominator. |
| `supporting_citations` | List each returned citation number whose own excerpt supports at least one substantive claim attached to it. Count each returned reference once. A different paper cannot establish an attributed finding of the target paper. |
| `all_claims_supported` | For each substantive response, record whether all material claims are supported by the passages cited where they occur. Material claims include scope, comparisons, causation and any claim of absence across the paper. A limitation explicitly scoped to the visible excerpts can be warranted. A response can present entirely grounded facts while leaving the question unanswered. For structured refusals and HTTP failures record `null`. |

Record a reason specific enough to locate the deciding claim and the evidence or missing detail. A `full` judgment requires `all_claims_supported=true`. Award no reference credit for a refusal. Official negatives with a source-supported answer keep their frozen label and are listed separately. A confirmed answer grounded in the named paper warrants `full` even on such an item. The official refusal and combined correctness metrics still count it as a miss.

Report whole-answer support as all-claims-supported substantive responses / all substantive responses, and individual citation support as supporting references / all returned references. For diagnostics comparing the same generator output under two service policies, judge the reconstructed pre-normalization answer with the same rubric. Primary totals count actual first HTTP responses only.
