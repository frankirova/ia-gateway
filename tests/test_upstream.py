"""Upstream failure translation: 502, 504, no retries, slot release."""

from __future__ import annotations

import http.client
import time
import urllib.error
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from conftest import (
    UPSTREAM_BASE_URL,
    FakeResponse,
    completion_bytes,
    gateway_document,
)


def assert_upstream_error(response: Any) -> None:
    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "message": "Upstream inference failed.",
            "type": "upstream_error",
            "param": None,
            "code": "upstream_error",
        }
    }
    assert response.headers["X-Request-ID"]


def assert_upstream_timeout(response: Any) -> None:
    assert response.status_code == 504
    assert response.json() == {
        "error": {
            "message": "Upstream inference timed out.",
            "type": "upstream_timeout",
            "param": None,
            "code": "upstream_timeout",
        }
    }
    assert response.headers["X-Request-ID"]


@pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
def test_non_success_upstream_status_becomes_502(
    client: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
    status: int,
) -> None:
    upstream.respond(completion_bytes(), status=status)

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert_upstream_error(response)
    assert upstream.call_count == 1


def test_http_error_from_backend_becomes_502(
    client: Any, chat: Any, upstream: Any, alpha_headers: dict[str, str]
) -> None:
    upstream.fail(
        urllib.error.HTTPError(
            f"{UPSTREAM_BASE_URL}/v1/chat/completions", 500, "boom", None, None
        )
    )

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert_upstream_error(response)


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b"",
        b"[]",
        b'{"choices": []}',
        b'{"choices": [{}]}',
        b'{"choices": [{"message": {}}]}',
        b'{"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}',
        b'{"choices": [{"message": {"content": "   "}, "finish_reason": "stop"}]}',
        b'{"choices": [{"message": {"content": "x"}}]}',
        b'{"choices": [{"message": {"content": "x"}, "finish_reason": "tool_calls"}]}',
        b'{"choices": [{"message": {"content": "x"}, "finish_reason": null}]}',
    ],
    ids=[
        "malformed-json",
        "empty-body",
        "json-array",
        "no-choices",
        "empty-choice",
        "no-message",
        "empty-content",
        "whitespace-content",
        "missing-finish-reason",
        "unsupported-finish-reason",
        "null-finish-reason",
    ],
)
def test_untrustworthy_upstream_body_becomes_502(
    client: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
    body: bytes,
) -> None:
    upstream.respond(body)

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert_upstream_error(response)


def test_oversized_upstream_body_becomes_502(
    client: Any, chat: Any, upstream: Any, alpha_headers: dict[str, str]
) -> None:
    huge = b'{"choices":[{"message":{"content":"' + b"x" * (1024 * 1024) + b'"}}]}'
    upstream.respond(huge)

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert_upstream_error(response)


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(OSError("connection refused")),
        ConnectionResetError("reset"),
        TimeoutError("timed out"),
        ValueError("invalid url"),
        http.client.BadStatusLine("garbage"),
    ],
    ids=[
        "url-error",
        "os-error",
        "timeout-error",
        "value-error",
        "http-exception",
    ],
)
def test_transport_failures_never_escape_as_500(
    client: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
    error: BaseException,
) -> None:
    upstream.fail(error)

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    if isinstance(error, TimeoutError):
        assert_upstream_timeout(response)
    else:
        assert_upstream_error(response)


def test_url_error_wrapping_timeout_becomes_504(
    client: Any, chat: Any, upstream: Any, alpha_headers: dict[str, str]
) -> None:
    upstream.fail(urllib.error.URLError(TimeoutError("read timed out")))

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert_upstream_timeout(response)


def test_deadline_breach_becomes_504(
    app_factory: Callable[..., Any],
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
) -> None:
    client = TestClient(
        app_factory(gateway_document(upstream={"read_timeout": 0.05}))
    )

    def slow_handler(_request: Any, _timeout: float | None) -> Any:
        time.sleep(0.2)
        return FakeResponse(completion_bytes())

    upstream.handler = slow_handler

    with client:
        response = client.post(
            "/v1/chat/completions", json=chat(), headers=alpha_headers
        )

    assert_upstream_timeout(response)
    assert upstream.call_count == 1


def test_upstream_failure_releases_the_inference_slot(
    client: Any, chat: Any, upstream: Any, alpha_headers: dict[str, str]
) -> None:
    upstream.fail(urllib.error.URLError(OSError("connection refused")))

    failed = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )
    upstream.handler = None
    recovered = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert failed.status_code == 502
    assert recovered.status_code == 200


def test_socket_timeout_covers_generation_not_just_connection(
    client_factory, upstream, chat, alpha_headers
):
    """A slow CPU backend must get the generation deadline, not the connect timeout.

    urllib cannot split connect from read timeouts, so the socket timeout has to
    be the generation deadline; otherwise any prompt slower than the connect
    timeout is misreported as a backend timeout.
    """
    client = client_factory(
        gateway_document(upstream={"connect_timeout": 1, "read_timeout": 120})
    )

    response = client.post("/v1/chat/completions", json=chat(), headers=alpha_headers)

    assert response.status_code == 200
    assert upstream.calls[0]["timeout"] == 120
