# ADR-008: Tool side-effect semantics

Status: Accepted in M0.

Tool effect, risk and approval are separate. Internal transactional tools use idempotency
keys. External effects without a confirmed outcome become `UNKNOWN_OUTCOME` and are never
silently retried; the corresponding Run becomes `NEEDS_ATTENTION`. Approval decision and
execution status are independent state dimensions.
