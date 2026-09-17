# ADR-001: Modular monolith plus worker

Status: Accepted in M0.

AgentHub remains one repository and one business deployment with API, worker and web
processes. This keeps cross-module semantics testable while allowing durable/background
work to run outside the request process. Multiple containers do not imply microservices.
