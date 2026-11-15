"""One gateway call, never retried.

The gateway owns model access: this module only speaks the OpenAI-shaped
completion endpoint with a bearer key read from a file. TLS verification is
never disabled: an ``https`` base URL requires a CA bundle. Errors carry a short
safe reason (status code or failure kind); the response body, the key and the CA
path contents never leave this module.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPSHandler,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

__all__ = [
    "COMPLETIONS_PATH",
    "GatewayError",
    "GatewaySettings",
    "complete",
    "endpoint",
    "extract_content",
    "read_application_key",
    "ssl_context_for",
]

COMPLETIONS_PATH = "/v1/chat/completions"

#: Hard cap on the response body read from the gateway; a completion is small.
MAX_RESPONSE_BYTES = 1_000_000

DEFAULT_MODEL = "e4b"
#: A measured reply for one finding is ~160 tokens, and the model truncates
#: mid-JSON when the budget is short, which then fails validation. The default
#: covers three findings with room to spare.
DEFAULT_MAX_TOKENS = 400
DEFAULT_TIMEOUT_SECONDS = 180.0

#: Signature of the diagnostics callback: status code (or ``None``) and seconds.
Diagnostics = Callable[[int | None, float], None]


class GatewayError(RuntimeError):
    """A gateway call failed. ``reason`` never contains secrets or raw bodies."""

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class GatewaySettings:
    """Everything needed to make one call, resolved from the environment."""

    base_url: str | None = None
    key_file: str | None = None
    ca_bundle: str | None = None
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


class _NoRedirect(HTTPRedirectHandler):
    """Refuse every redirect: the gateway never redirects and keys stay put."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _scheme(url: str) -> str:
    return urlsplit(url).scheme.lower()


def ssl_context_for(url: str, ca_bundle: str | None) -> ssl.SSLContext | None:
    """Verifying context for ``https``; ``None`` for a plain ``http`` URL."""
    if _scheme(url) != "https":
        return None
    if ca_bundle is None:
        raise GatewayError("an https gateway URL requires a CA bundle")
    return ssl.create_default_context(cafile=ca_bundle)


def endpoint(base_url: str) -> str:
    """Append the completions path to the configured base URL."""
    return base_url.rstrip("/") + COMPLETIONS_PATH


def read_application_key(key_file: str | None) -> str:
    """Read the bearer key from its file; never from a command-line flag."""
    if key_file is None:
        raise GatewayError("the application key file is missing")
    path = Path(key_file)
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise GatewayError("the application key file is missing") from None
    if not key:
        raise GatewayError("the application key file is empty")
    return key


def _content_of(document: Any) -> str:
    if not isinstance(document, dict):
        raise GatewayError("the gateway response is not a JSON object")
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise GatewayError("the gateway response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise GatewayError("the gateway response has no choices")
    message = first.get("message")
    if not isinstance(message, dict):
        raise GatewayError("the gateway response has no message")
    content = message.get("content")
    if not isinstance(content, str):
        raise GatewayError("the gateway response has no message content")
    return content


def extract_content(raw: bytes) -> str:
    """Decode the response body and return the assistant message content."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayError("the gateway response is not JSON") from None
    return _content_of(document)


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        return f"gateway returned HTTP {error.code}"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "gateway request timed out"
    if isinstance(error, URLError):
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return "gateway request timed out"
        return "gateway connection failed"
    return "gateway request failed"


def complete(
    settings: GatewaySettings,
    messages: list[dict[str, str]],
    *,
    diagnostics: Diagnostics | None = None,
) -> str:
    """Make exactly one non-streaming completion call and return its content.

    Raises :class:`GatewayError` for every failure. Nothing is retried and TLS
    verification is never relaxed.
    """
    if settings.base_url is None:
        raise GatewayError("the gateway URL is not configured")
    url = endpoint(settings.base_url)
    key = read_application_key(settings.key_file)
    context = ssl_context_for(url, settings.ca_bundle)

    body = json.dumps(
        {
            "model": settings.model,
            "messages": messages,
            "max_tokens": settings.max_tokens,
            "temperature": 0,
            "reasoning": False,
            "stream": False,
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    handlers: list[Any] = [_NoRedirect()]
    if context is not None:
        handlers.append(HTTPSHandler(context=context))
    opener = build_opener(*handlers)
    status: int | None = None
    started = time.monotonic()
    try:
        with opener.open(request, timeout=settings.timeout_seconds) as response:
            status = getattr(response, "status", 200)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, OSError) as error:
        status = error.code if isinstance(error, HTTPError) else None
        reason = _failure_reason(error)
        if isinstance(error, HTTPError):
            error.close()
        raise GatewayError(reason, status=status) from None
    finally:
        if diagnostics is not None:
            diagnostics(status, time.monotonic() - started)

    if len(raw) > MAX_RESPONSE_BYTES:
        raise GatewayError("the gateway response is too large")
    return extract_content(raw)
