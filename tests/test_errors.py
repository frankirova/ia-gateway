"""Cross-cutting error envelope guarantees."""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from app import upstream as upstream_adapter
from conftest import (
    UPSTREAM_BASE_URL,
    application_entry,
    completion_bytes,
    gateway_document,
)

ERROR_ENVELOPE_KEYS = {"message", "type", "param", "code"}


def assert_envelope(response: Any, *, code: str, status: int) -> None:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == ERROR_ENVELOPE_KEYS
    assert body["error"]["code"] == code
    assert response.headers.get("X-Request-ID")


def test_request_id_present_on_success(
    client: Any, chat: Any, upstream: Any, alpha_headers: dict[str, str]
) -> None:
    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("req_")


def test_request_id_is_unique_per_request(
    client: Any, chat: Any, upstream: Any, alpha_headers: dict[str, str]
) -> None:
    first = client.post("/v1/chat/completions", json=chat(), headers=alpha_headers)
    second = client.post("/v1/chat/completions", json=chat(), headers=alpha_headers)

    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid_request"),
        (401, "invalid_api_key"),
        (413, "payload_too_large"),
        (415, "unsupported_media_type"),
    ],
)
def test_client_errors_use_the_envelope(
    client: Any,
    chat: Any,
    alpha_headers: dict[str, str],
    status: int,
    code: str,
) -> None:
    requests = {
        400: dict(json=chat(top_p=0.9), headers=alpha_headers),
        401: dict(json=chat()),
        413: dict(
            content=b"x" * (64 * 1024 + 1),
            headers={**alpha_headers, "Content-Type": "application/json"},
        ),
        415: dict(
            content=b"{}", headers={**alpha_headers, "Content-Type": "text/plain"}
        ),
    }

    response = client.post("/v1/chat/completions", **requests[status])

    assert_envelope(response, code=code, status=status)


def test_model_not_allowed_uses_the_envelope(
    client_factory: Any, chat: Any, upstream: Any, alpha_headers: dict[str, str]
) -> None:
    client = client_factory(
        gateway_document(applications=[application_entry(models=["other"])])
    )

    response = client.post(
        "/v1/chat/completions", json=chat(model="e4b"), headers=alpha_headers
    )

    assert_envelope(response, code="model_not_allowed", status=403)
    assert upstream.call_count == 0


def test_upstream_errors_use_the_envelope(
    client: Any, chat: Any, upstream: Any, alpha_headers: dict[str, str]
) -> None:
    upstream.fail(urllib.error.URLError(OSError("connection refused")))

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert_envelope(response, code="upstream_error", status=502)
    assert UPSTREAM_BASE_URL not in response.text
    assert "connection refused" not in response.text


def test_deadline_uses_the_envelope(
    client: Any, chat: Any, upstream: Any, alpha_headers: dict[str, str]
) -> None:
    upstream.fail(urllib.error.URLError(TimeoutError("timed out")))

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert_envelope(response, code="upstream_timeout", status=504)


def test_unexpected_failure_returns_internal_error_without_details(
    client: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("internal diagnostic that must not leak")

    monkeypatch.setattr(upstream_adapter, "chat_completion", explode)

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert_envelope(response, code="internal_error", status=500)
    assert "internal diagnostic" not in response.text


def test_unknown_endpoint_uses_the_envelope(client: Any) -> None:
    response = client.get("/v1/unknown")

    assert response.status_code == 404
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == ERROR_ENVELOPE_KEYS
    assert response.headers.get("X-Request-ID")


@pytest.mark.parametrize(
    "request_kwargs",
    [
        dict(json={"model": "e4b"}),
        dict(json={"model": "e4b", "messages": [], "top_p": 1}),
        dict(content=b"{oops", headers={"Content-Type": "application/json"}),
        dict(content=b"[]", headers={"Content-Type": "application/json"}),
    ],
)
def test_validation_never_leaks_the_framework_schema(
    client: Any,
    alpha_headers: dict[str, str],
    request_kwargs: dict[str, Any],
) -> None:
    headers = {**alpha_headers, **request_kwargs.pop("headers", {})}

    response = client.post("/v1/chat/completions", headers=headers, **request_kwargs)

    assert response.status_code != 422
    assert response.status_code == 400
    assert "detail" not in response.json()
    assert set(response.json()) == {"error"}


def test_error_messages_never_echo_the_presented_key(
    client: Any, chat: Any
) -> None:
    presented = "totally-unknown-key-" + "9" * 40

    response = client.post(
        "/v1/chat/completions",
        json=chat(),
        headers={"Authorization": f"Bearer {presented}"},
    )

    assert presented not in response.text


def test_success_response_contains_the_gateway_id_not_the_backend_id(
    client: Any, chat: Any, upstream: Any, alpha_headers: dict[str, str]
) -> None:
    upstream.respond(completion_bytes())

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )

    assert response.status_code == 200
    assert response.json()["id"] != "upstream-1"
