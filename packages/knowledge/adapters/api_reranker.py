"""OpenAI/DashScope-compatible HTTP cross-encoder reranker adapter."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from packages.knowledge.contracts import KnowledgeProviderError, RerankCandidate


class OpenAICompatibleReranker:
    """HTTP adapter implementing :class:`Reranker` for compatible rerank APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.model_name = model_name.strip()
        self.timeout_seconds = float(timeout_seconds)
        self._client = client

    def _endpoint(self) -> str:
        if self.base_url.endswith("/reranks") or self.base_url.endswith("/text-rerank"):
            return self.base_url
        return f"{self.base_url}/reranks"

    def _build_payload(self, query: str, documents: list[str]) -> dict[str, Any]:
        if self.base_url.endswith("/text-rerank"):
            return {
                "model": self.model_name,
                "input": {"query": query, "documents": documents},
                "parameters": {"return_documents": False, "top_n": len(documents)},
            }
        return {
            "model": self.model_name,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
        }

    def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> tuple[float, ...]:
        if not candidates:
            return ()
        documents = [candidate.text for candidate in candidates]
        payload = self._build_payload(query, documents)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            if self._client is not None:
                response = self._client.post(self._endpoint(), headers=headers, json=payload)
            else:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.post(self._endpoint(), headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise KnowledgeProviderError(
                "RERANKER_INFERENCE_FAILED",
                "The reranker API request failed.",
            ) from exc

        if response.status_code != 200:
            raise KnowledgeProviderError(
                "RERANKER_INFERENCE_FAILED",
                f"The reranker API returned HTTP {response.status_code}.",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise KnowledgeProviderError(
                "INVALID_RERANK_RESULT",
                "The reranker API returned invalid JSON.",
            ) from exc

        if not isinstance(body, Mapping):
            raise KnowledgeProviderError(
                "INVALID_RERANK_RESULT",
                "The reranker API returned an invalid response.",
            )
        raw_results = body.get("results")
        if raw_results is None and isinstance(body.get("output"), Mapping):
            raw_results = body["output"].get("results")
        if not isinstance(raw_results, list) or len(raw_results) != len(candidates):
            raise KnowledgeProviderError(
                "INVALID_RERANK_RESULT",
                "The reranker API returned an unexpected result count.",
            )

        scores: dict[int, float] = {}
        for item in raw_results:
            if not isinstance(item, Mapping):
                raise KnowledgeProviderError(
                    "INVALID_RERANK_RESULT",
                    "The reranker API returned a malformed item.",
                )
            idx = item.get("index")
            raw_score = item.get("relevance_score")
            if type(idx) is not int or not 0 <= idx < len(candidates) or idx in scores:
                raise KnowledgeProviderError(
                    "INVALID_RERANK_RESULT",
                    "The reranker API returned invalid candidate indices.",
                )
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise KnowledgeProviderError(
                    "INVALID_RERANK_RESULT",
                    "The reranker API returned a non-numeric score.",
                )
            score = float(raw_score)
            if not math.isfinite(score):
                raise KnowledgeProviderError(
                    "INVALID_RERANK_RESULT",
                    "The reranker API returned a non-finite score.",
                )
            scores[idx] = score

        return tuple(scores[i] for i in range(len(candidates)))


__all__ = ["OpenAICompatibleReranker"]
