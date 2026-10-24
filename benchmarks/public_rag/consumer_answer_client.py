"""One-shot consumer answer generation for the fixed public RAG experiment."""

from __future__ import annotations

import json
import time

import httpx

EXPERIMENT_MODEL = "deepseek-v4.1-flash-1017"


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _strict_json_loads(value: str | bytes) -> object:
    return json.loads(
        value,
        object_pairs_hook=_object_without_duplicate_keys,
        parse_constant=_reject_nonstandard_constant,
    )


def _observed_usage(value: object) -> dict[str, object] | None:
    if type(value) is not dict:
        return None

    output: dict[str, object] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        count = value.get(key)
        if type(count) is int and count >= 0:
            output[key] = count

    for key in (
        "prompt_tokens_details",
        "completion_tokens_details",
        "total_tokens_details",
    ):
        details = value.get(key)
        if type(details) is not dict:
            continue
        observed = {
            name: count
            for name, count in details.items()
            if type(name) is str and type(count) is int and count >= 0
        }
        if observed:
            output[key] = observed
    return output or None


def _parse_answer(content: object) -> dict[str, object]:
    if type(content) is not str:
        raise ValueError("message content must be JSON text")

    payload = _strict_json_loads(content)
    expected = {"answer", "unanswerable", "cited_chunk_ids"}
    if type(payload) is not dict or set(payload) != expected:
        raise ValueError("answer must be an object with exactly three keys")

    answer = payload["answer"]
    unanswerable = payload["unanswerable"]
    cited_chunk_ids = payload["cited_chunk_ids"]
    if type(answer) is not str:
        raise ValueError("answer must be a string")
    if type(unanswerable) is not bool:
        raise ValueError("unanswerable must be a boolean")
    if type(cited_chunk_ids) is not list or any(
        type(chunk_id) is not str or not chunk_id.strip() for chunk_id in cited_chunk_ids
    ):
        raise ValueError("cited_chunk_ids must be a list of strings")
    if unanswerable and answer != "":
        raise ValueError("an unanswerable response must have an empty answer")
    if not unanswerable and not answer.strip():
        raise ValueError("an answerable response must have a non-empty answer")

    return {
        "answer": answer,
        "unanswerable": unanswerable,
        "cited_chunk_ids": cited_chunk_ids,
    }


async def generate_once(
    messages: list[dict],
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
) -> dict:
    """Make one chat-completions request and strictly classify its outcome."""
    payload = {
        "model": EXPERIMENT_MODEL,
        "temperature": 0,
        "max_tokens": 256,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    result: dict[str, object] = {
        "status": "INVALID_OUTPUT",
        "request_ms": 0.0,
        "usage": None,
    }
    started = time.perf_counter()
    try:
        response = await client.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key},
            json=payload,
            follow_redirects=False,
        )
    except httpx.RequestError:
        result["status"] = "UNKNOWN_OUTCOME"
        return result
    finally:
        result["request_ms"] = (time.perf_counter() - started) * 1000

    if not response.is_success:
        result.update(status="PROVIDER_ERROR", http_status=response.status_code)
        return result

    try:
        body = _strict_json_loads(response.content)
        if type(body) is not dict:
            raise ValueError("response body must be an object")
        result["usage"] = _observed_usage(body.get("usage"))
        if body.get("model") != EXPERIMENT_MODEL:
            result["status"] = "MODEL_IDENTITY_ERROR"
            return result
        choices = body["choices"]
        if type(choices) is not list or not choices:
            raise ValueError("choices must be a non-empty list")
        choice = choices[0]
        if type(choice) is not dict:
            raise ValueError("choice must be an object")
        message = choice["message"]
        if type(message) is not dict:
            raise ValueError("choice message must be an object")
        parsed = _parse_answer(message["content"])
        result.update(status="VALID", **parsed)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        result["status"] = "INVALID_OUTPUT"
    return result
