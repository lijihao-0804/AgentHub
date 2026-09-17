# ADR-004: ModelGateway and LiteLLM

Status: Accepted in M0.

Provider differences are hidden behind `ModelGateway`. LiteLLM will be used by a later
adapter for provider normalization, retry/fallback and usage/cost reporting. Business code
will not import provider SDK types.
