# M7 Integration Closure

Status: implementation complete; final acceptance is pending exact-head CI for this closure.

## Baseline and frontend history

- Backend baseline: `093c2429aa824b63ba305455fdbe7cee896bd94e`
- Frontend productization: `3afd5743955d60246ee7c35237b98c2aa25a9416`
- Frontend i18n: `c2825d703acac0ca7e0ed526d156f8eb2709187f`
- Frontend integration merge: `8dac4bac1a5207b158877ef878efef0a7625934e`
- Merge conflicts: none

## Closure scope

- Comparison creation handles concurrent unique-key races by rolling back, re-reading the
  existing artifact, and re-validating its hash, snapshot binding, and variant bindings.
- Cost reporting keeps execution-level cost metrics, adds successful dataset-item cost metrics
  with mean known repetition costs, and records `cost_per_successful_case` as a deprecated
  explicit alias.
- Frontend status badges expose their visible localized label without an overriding container
  aria label. The root document uses `en-US`; simplified Chinese browser locales map to `zh-CN`
  while Traditional Chinese locales remain `en-US`.
- The existing locale key is the only browser persistence used by the web application; tokens,
  credentials, and session state are not persisted by this closure.
- M7-D review documentation now records the correct full review baseline SHA.

## Verification notes

- Migration head remains `0020_m7d_review_closure`; no new migration was added.
- Frontend TypeScript validation and production build pass.
- The real browser visual check was not run in this non-browser validation pass:
  `VISUAL_BROWSER_CHECK=NOT_RUN`.
