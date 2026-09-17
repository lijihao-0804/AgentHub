from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from packages.core.errors.exceptions import AgentHubError
from packages.core.errors.models import ErrorBody, ErrorEnvelope

logger = logging.getLogger(__name__)


def error_response(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    payload = ErrorEnvelope(
        error=ErrorBody(code=code, message=message, request_id=request_id)
    ).model_dump()
    return JSONResponse(status_code=status_code, content=payload)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AgentHubError)
    async def handle_agenthub_error(request: Request, exc: AgentHubError) -> JSONResponse:
        return error_response(request, exc.code, exc.message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del exc
        return error_response(request, "VALIDATION_ERROR", "Request validation failed.", 422)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_request_error", exc_info=exc)
        return error_response(request, "INTERNAL_ERROR", "An unexpected error occurred.", 500)
