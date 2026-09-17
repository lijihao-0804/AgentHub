# ADR-005: Business run data and trace data are separate

Status: Accepted in M0.

AgentHub owns durable Run/Step business records beginning in M4. OTel/Langfuse receives
safe projections from a `TraceSink`; it is optional, content capture is opt-in, and its
availability cannot make core business requests fail.
