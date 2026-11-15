"""Adapter for the llama.cpp OpenAI-compatible backend.

The gateway owns the backend address, the model name and the authentication, so
task clients never see them. Transport failures are translated into the two
upstream error kinds the public contract exposes; nothing is retried.
"""

from __future__ import annotations

import http.client
import json
import time
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import UpstreamConfig

MAX_UPSTREAM_BODY_BYTES = 1024 * 1024
ACCEPTED_FINISH_REASONS = frozenset({"stop", "length"})
HEALTH_TIMEOUT_SECONDS = 2.0


class _RedirectRefused(HTTPRedirectHandler):
    """Refuse every upstream redirect instead of re-sending credentials elsewhere."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_RedirectRefused())


def urlopen(request: Request, timeout: float | None = None) -> Any:
    """Open an upstream request without following redirects.

    This module-level seam keeps tests in control of the transport while making
    sure a 3xx never replays the credential or the prompt at another address.
    """
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


class UpstreamError(Exception):
    """The backend failed or returned something the gateway cannot trust."""


class UpstreamTimeout(UpstreamError):
    """The backend exceeded the configured generation deadline."""


@dataclass(frozen=True)
class Completion:
    content: str
    finish_reason: str
    usage: dict[str, int] | None


def chat_completion(
    upstream: UpstreamConfig,
    model: str,
    messages: Sequence[dict[str, str]],
    max_tokens: int,
    *,
    temperature: float = 0,
    reasoning: bool = True,
) -> Completion:
    """Perform one non-streaming completion request against the backend."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "max_tokens": max_tokens,
        # A plain number, which the backend understands as-is.
        "temperature": temperature,
        "stream": False,
    }
    if not reasoning:
        # The only supported way to ask llama.cpp to skip reasoning tokens for a
        # thinking model. It is template-dependent, so it is a caller-requested
        # lever and never an assumption the gateway makes.
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if upstream.api_key:
        headers["Authorization"] = f"Bearer {upstream.api_key}"

    request = Request(
        upstream.chat_completions_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    # urllib cannot split connect from read timeouts: the socket timeout must
    # cover the whole generation, and the elapsed check below enforces the rest.
    timeout = upstream.read_timeout
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if not isinstance(status, int) or status < 200 or status >= 300:
                raise UpstreamError("upstream returned a non-success status")
            raw = response.read(MAX_UPSTREAM_BODY_BYTES + 1)
    except UpstreamError:
        raise
    except URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise UpstreamTimeout("upstream connection timed out") from exc
        raise UpstreamError("upstream connection failed") from exc
    except TimeoutError as exc:
        raise UpstreamTimeout("upstream deadline exceeded") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise UpstreamError("upstream transport failed") from exc

    if time.monotonic() - started > upstream.read_timeout:
        raise UpstreamTimeout("upstream deadline exceeded")
    if len(raw) > MAX_UPSTREAM_BODY_BYTES:
        raise UpstreamError("upstream response exceeded the size limit")

    return _parse_completion(raw)


def check_health(upstream: UpstreamConfig) -> bool:
    """Probe the backend health endpoint; never performs generation."""
    request = Request(upstream.health_url, method="GET")
    timeout = min(upstream.connect_timeout, HEALTH_TIMEOUT_SECONDS)
    try:
        with urlopen(request, timeout=timeout) as response:
            return getattr(response, "status", 200) == 200
    except (URLError, OSError, ValueError, http.client.HTTPException):
        return False


def _parse_completion(raw: bytes) -> Completion:
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise UpstreamError("upstream response was not valid JSON") from exc
    if not isinstance(document, dict):
        raise UpstreamError("upstream response was not a JSON object")

    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise UpstreamError("upstream response contained no choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise UpstreamError("upstream choice was not an object")

    message = choice.get("message")
    if not isinstance(message, dict):
        raise UpstreamError("upstream choice contained no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise UpstreamError("upstream response contained no content")

    finish_reason = choice.get("finish_reason")
    if not isinstance(finish_reason, str) or finish_reason not in ACCEPTED_FINISH_REASONS:
        raise UpstreamError("upstream returned an unsupported finish reason")

    return Completion(
        content=content,
        finish_reason=finish_reason,
        usage=_parse_usage(document.get("usage")),
    )


def _parse_usage(raw: Any) -> dict[str, int] | None:
    """Return trustworthy integer counts, or ``None`` so the gateway omits them."""
    if not isinstance(raw, dict):
        return None
    counts: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        counts[key] = value
    return counts
