"""The stable error envelope returned by every gateway endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RETRY_AFTER_SECONDS = 5
INVALID_API_KEY_MESSAGE = "Missing or invalid application credential."


@dataclass
class GatewayError(Exception):
    """A client-visible failure with a stable code and no internal detail."""

    status_code: int
    code: str
    message: str
    type: str
    param: str | None = None
    retry_after: int | None = None

    def __str__(self) -> str:  # pragma: no cover - defensive
        return self.message

    def envelope(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.type,
                "param": self.param,
                "code": self.code,
            }
        }

    def headers(self) -> dict[str, str]:
        if self.retry_after is None:
            return {}
        return {"Retry-After": str(self.retry_after)}


def invalid_request(message: str, param: str | None = None) -> GatewayError:
    return GatewayError(400, "invalid_request", message, "invalid_request", param)


def invalid_api_key() -> GatewayError:
    return GatewayError(
        401, "invalid_api_key", INVALID_API_KEY_MESSAGE, "authentication_error"
    )


def model_not_allowed() -> GatewayError:
    return GatewayError(
        403,
        "model_not_allowed",
        "The requested model is not permitted for this application.",
        "permission_error",
    )


def payload_too_large() -> GatewayError:
    return GatewayError(
        413,
        "payload_too_large",
        "Request body exceeds the gateway limit.",
        "invalid_request",
    )


def unsupported_media_type() -> GatewayError:
    return GatewayError(
        415,
        "unsupported_media_type",
        "Request body must be JSON with content type application/json.",
        "invalid_request",
    )


def rate_limit_exceeded() -> GatewayError:
    return GatewayError(
        429,
        "rate_limit_exceeded",
        "Application request-rate limit reached.",
        "rate_limit_error",
        retry_after=RETRY_AFTER_SECONDS,
    )


def capacity_exhausted() -> GatewayError:
    return GatewayError(
        503,
        "capacity_exhausted",
        "Inference capacity is currently full.",
        "service_unavailable",
        retry_after=RETRY_AFTER_SECONDS,
    )


def upstream_failure() -> GatewayError:
    return GatewayError(
        502,
        "upstream_error",
        "Upstream inference failed.",
        "upstream_error",
    )


def upstream_deadline() -> GatewayError:
    return GatewayError(
        504,
        "upstream_timeout",
        "Upstream inference timed out.",
        "upstream_timeout",
    )


def internal_error() -> GatewayError:
    return GatewayError(
        500, "internal_error", "Unexpected gateway failure.", "internal_error"
    )
