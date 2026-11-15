"""Body limits, media type, admission control, rate limiting and context budget."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from conftest import (
    ALPHA_KEY,
    BETA_KEY,
    FakeResponse,
    application_entry,
    completion_bytes,
    gateway_document,
)

MAX_BODY_BYTES = 64 * 1024


def test_oversized_body_is_rejected(
    client: Any, alpha_headers: dict[str, str], upstream: Any
) -> None:
    body = b'{"model":"e4b","messages":[{"role":"user","content":"' + b"a" * MAX_BODY_BYTES + b'"}]}'

    response = client.post(
        "/v1/chat/completions",
        content=body,
        headers={**alpha_headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert response.json()["error"]["param"] is None
    assert response.headers["X-Request-ID"]
    assert upstream.call_count == 0


def test_oversized_chunked_body_is_rejected_while_reading(
    client: Any, alpha_headers: dict[str, str], upstream: Any
) -> None:
    def chunks():
        for _ in range(9):
            yield b"x" * 8192

    response = client.post(
        "/v1/chat/completions",
        content=chunks(),
        headers={**alpha_headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert upstream.call_count == 0


def test_body_at_the_limit_is_not_rejected_for_size(
    client: Any, alpha_headers: dict[str, str], upstream: Any
) -> None:
    prefix = b'{"model":"e4b","messages":[]}'
    body = prefix + b" " * (MAX_BODY_BYTES - len(prefix))

    response = client.post(
        "/v1/chat/completions",
        content=body,
        headers={**alpha_headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "application/json-patch+json"],
)
def test_non_json_content_type_is_rejected(
    client: Any,
    alpha_headers: dict[str, str],
    upstream: Any,
    content_type: str,
) -> None:
    response = client.post(
        "/v1/chat/completions",
        content=b'{"model":"e4b","messages":[{"role":"user","content":"hi"}]}',
        headers={**alpha_headers, "Content-Type": content_type},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert upstream.call_count == 0


def test_missing_content_type_is_rejected(
    client: Any, alpha_headers: dict[str, str], upstream: Any
) -> None:
    response = client.post(
        "/v1/chat/completions",
        content=b'{"model":"e4b","messages":[{"role":"user","content":"hi"}]}',
        headers=alpha_headers,
    )

    assert response.status_code == 415
    assert upstream.call_count == 0


def test_json_content_type_with_charset_is_accepted(
    client: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
) -> None:
    response = client.post(
        "/v1/chat/completions",
        content=json.dumps(chat()).encode("utf-8"),
        headers={**alpha_headers, "Content-Type": "application/json; charset=utf-8"},
    )

    assert response.status_code == 200


def test_rate_limit_exhaustion_returns_429(
    client_factory: Callable[..., Any],
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
) -> None:
    client = client_factory(
        gateway_document(
            applications=[
                application_entry(rate_capacity=2, rate_per_minute=1),
            ]
        )
    )

    first = client.post("/v1/chat/completions", json=chat(), headers=alpha_headers)
    second = client.post("/v1/chat/completions", json=chat(), headers=alpha_headers)
    third = client.post("/v1/chat/completions", json=chat(), headers=alpha_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "rate_limit_exceeded"
    assert third.json()["error"]["param"] is None
    assert third.headers["Retry-After"] == "5"
    assert upstream.call_count == 2


def test_rate_limit_is_per_application(
    client_factory: Callable[..., Any],
    chat: Any,
    upstream: Any,
) -> None:
    client = client_factory(
        gateway_document(
            applications=[
                application_entry("alpha", ALPHA_KEY, rate_capacity=1, rate_per_minute=1),
                application_entry("beta", BETA_KEY, rate_capacity=1, rate_per_minute=1),
            ]
        )
    )
    alpha = {"Authorization": f"Bearer {ALPHA_KEY}"}
    beta = {"Authorization": f"Bearer {BETA_KEY}"}

    assert client.post("/v1/chat/completions", json=chat(), headers=alpha).status_code == 200
    assert client.post("/v1/chat/completions", json=chat(), headers=beta).status_code == 200
    assert client.post("/v1/chat/completions", json=chat(), headers=alpha).status_code == 429


def test_busy_inference_slot_returns_503_and_recovers(
    app: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def slow_handler(_request: Any, _timeout: float | None) -> Any:
        entered.set()
        release.wait(timeout=10)
        return FakeResponse(completion_bytes())

    upstream.handler = slow_handler
    results: dict[str, Any] = {}

    with TestClient(app) as in_flight, TestClient(app) as contender:
        def first_request() -> None:
            results["first"] = in_flight.post(
                "/v1/chat/completions", json=chat(), headers=alpha_headers
            )

        thread = threading.Thread(target=first_request)
        thread.start()
        assert entered.wait(timeout=10)

        busy = contender.post(
            "/v1/chat/completions", json=chat(), headers=alpha_headers
        )

        release.set()
        thread.join(timeout=10)

        recovered = contender.post(
            "/v1/chat/completions", json=chat(), headers=alpha_headers
        )

    assert busy.status_code == 503
    assert busy.json()["error"]["code"] == "capacity_exhausted"
    assert busy.json()["error"]["param"] is None
    assert busy.headers["Retry-After"] == "5"
    assert results["first"].status_code == 200
    assert recovered.status_code == 200


def test_capacity_rejection_consumes_a_rate_limit_token(
    app_factory: Callable[..., Any],
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
) -> None:
    app = app_factory(
        gateway_document(
            applications=[
                application_entry(rate_capacity=3, rate_per_minute=1),
            ]
        )
    )
    entered = threading.Event()
    release = threading.Event()

    def slow_handler(_request: Any, _timeout: float | None) -> Any:
        entered.set()
        release.wait(timeout=10)
        return FakeResponse(completion_bytes())

    upstream.handler = slow_handler

    with TestClient(app) as in_flight, TestClient(app) as contender:
        def first_request() -> None:
            in_flight.post("/v1/chat/completions", json=chat(), headers=alpha_headers)

        thread = threading.Thread(target=first_request)
        thread.start()
        assert entered.wait(timeout=10)

        first_busy = contender.post(
            "/v1/chat/completions", json=chat(), headers=alpha_headers
        )
        second_busy = contender.post(
            "/v1/chat/completions", json=chat(), headers=alpha_headers
        )
        exhausted = contender.post(
            "/v1/chat/completions", json=chat(), headers=alpha_headers
        )

        release.set()
        thread.join(timeout=10)

    assert first_busy.status_code == 503
    assert second_busy.status_code == 503
    assert exhausted.status_code == 429
    assert exhausted.json()["error"]["code"] == "rate_limit_exceeded"


def test_context_character_budget_rejects_oversized_messages(
    client_factory: Callable[..., Any],
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
) -> None:
    client = client_factory(gateway_document(limits={"context_char_budget": 200}))

    response = client.post(
        "/v1/chat/completions",
        json=chat(messages=[{"role": "user", "content": "a" * 500}]),
        headers=alpha_headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["param"] == "messages"
    assert upstream.call_count == 0


def test_context_character_budget_allows_messages_within_budget(
    client_factory: Callable[..., Any],
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
) -> None:
    client = client_factory(gateway_document(limits={"context_char_budget": 200}))

    response = client.post(
        "/v1/chat/completions",
        json=chat(messages=[{"role": "user", "content": "a" * 50}]),
        headers=alpha_headers,
    )

    assert response.status_code == 200
    assert upstream.call_count == 1
