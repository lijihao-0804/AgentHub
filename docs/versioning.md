# Versioning and delivery

> **2026-10-05 当前状态**：[功能与证据口径](current-state.md)。T12/T28 已关闭，MCP/Auth UI、反馈、人工接管和专项前端脚本已存在；功能开发已收口，旧计划未完成项为 DEFERRED / FUTURE WORK。历史验收数字按各自日期/SHA 解读。

The repository uses Git with milestone-scoped commits. Commit messages use:

```text
M0: <completed change>
M1: <completed change>
```

Before a milestone commit: run the milestone checklist, inspect `git diff --check`, run
tests/lint/build, and record any environment-only limitation. A milestone is not accepted
when only the code exists; its contracts and verification record must be present too.

## Current delivery state

Feature development stopped after the 2026-10-05 closure (`e7dc1f6` in main). Deferred plans are not a queue of implicitly authorized milestones.
Docs-only changes update current navigation and clarify historical baselines; their relevant checks are links, referenced paths, scope and `git diff --check`.
They do not require repeating historical paid models, load tests or browser matrices. Existing GitHub CI may still trigger on a push; that automation is separate from a claim of a new local experiment.
Keep original acceptance reports and frozen artifacts intact. Commit subjects follow the existing history (including conventional `docs:`, `test:`, `fix:` prefixes); milestone labels are not the only valid format.
