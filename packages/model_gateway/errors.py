"""Safe, provider-independent model gateway errors."""

from enum import StrEnum


class ModelGatewayErrorCode(StrEnum):
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
    MODEL_PROVIDER_UNAVAILABLE = "MODEL_PROVIDER_UNAVAILABLE"
    MODEL_BAD_RESPONSE = "MODEL_BAD_RESPONSE"
    MODEL_CAPABILITY_MISMATCH = "MODEL_CAPABILITY_MISMATCH"
    MODEL_PROFILE_DISABLED = "MODEL_PROFILE_DISABLED"
    MODEL_STREAM_INTERRUPTED = "MODEL_STREAM_INTERRUPTED"


_SAFE_MESSAGES = {
    ModelGatewayErrorCode.MODEL_TIMEOUT: "The model request timed out.",
    ModelGatewayErrorCode.MODEL_RATE_LIMITED: "The model provider rate-limited the request.",
    ModelGatewayErrorCode.MODEL_AUTH_FAILED: "The model provider rejected authentication.",
    ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE: "The model provider is unavailable.",
    ModelGatewayErrorCode.MODEL_BAD_RESPONSE: "The model provider returned an invalid response.",
    ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH: (
        "The model profile lacks a required capability."
    ),
    ModelGatewayErrorCode.MODEL_PROFILE_DISABLED: "The model profile is disabled.",
    ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED: "The model stream was interrupted.",
}


class ModelGatewayError(Exception):
    """An error safe to cross the application boundary."""

    def __init__(
        self,
        code: ModelGatewayErrorCode,
        *,
        retryable: bool = False,
        message: str | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.message = message or _SAFE_MESSAGES[code]
        super().__init__(self.message)


__all__ = ["ModelGatewayError", "ModelGatewayErrorCode"]
