# Versioning and delivery

The repository uses Git with milestone-scoped commits. Commit messages use:

```text
M0: <completed change>
M1: <completed change>
```

Before a milestone commit: run the milestone checklist, inspect `git diff --check`, run
tests/lint/build, and record any environment-only limitation. A milestone is not accepted
when only the code exists; its contracts and verification record must be present too.
