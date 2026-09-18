"""Safe errors raised by builtin tool handlers."""

from __future__ import annotations


class ToolHandlerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = ["ToolHandlerError"]
