from __future__ import annotations

from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from packages.core.logging.json_logging import request_id_var, trace_id_var

_MAX_ID_LENGTH = 128
_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_HEADER_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'*+-.^_`|~"
)


def _safe_identifier(value: str) -> str | None:
    if (
        not value
        or len(value) > _MAX_ID_LENGTH
        or value[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        or any(character not in _ID_CHARS for character in value)
    ):
        return None
    return value


def _header_name(value: str) -> bytes:
    candidate = value.strip()
    if not candidate or any(character not in _HEADER_NAME_CHARS for character in candidate):
        return b"x-request-id"
    try:
        return candidate.lower().encode("ascii")
    except UnicodeEncodeError:
        return b"x-request-id"


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp, request_id_header: str = "X-Request-ID") -> None:
        self.app = app
        self.request_id_header = _header_name(request_id_header)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        request_id = _safe_identifier(
            headers.get(self.request_id_header, b"").decode("latin-1").strip()
        ) or str(uuid4())
        trace_id = _safe_identifier(
            headers.get(b"x-trace-id", b"").decode("latin-1").strip()
        ) or request_id
        scope["state"] = {**scope.get("state", {}), "request_id": request_id, "trace_id": trace_id}
        request_token = request_id_var.set(request_id)
        trace_token = trace_id_var.set(trace_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((self.request_id_header, request_id.encode("ascii")))
                message = {**message, "headers": response_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            trace_id_var.reset(trace_token)
            request_id_var.reset(request_token)
