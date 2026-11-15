"""Application bearer authentication and the /v1/models scope."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import ALPHA_KEY, INVALID_API_KEY_MESSAGE


def assert_invalid_api_key(response: Any) -> None:
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "message": INVALID_API_KEY_MESSAGE,
            "type": "authentication_error",
            "param": None,
            "code": "invalid_api_key",
        }
    }
    assert response.headers["X-Request-ID"]


def test_missing_authorization_is_rejected(client: Any, chat: Any, upstream: Any) -> None:
    response = client.post("/v1/chat/completions", json=chat())

    assert_invalid_api_key(response)
    assert upstream.call_count == 0


def test_unknown_key_is_rejected(client: Any, chat: Any, upstream: Any) -> None:
    response = client.post(
        "/v1/chat/completions",
        json=chat(),
        headers={"Authorization": "Bearer " + "unknown-key-" + "0" * 48},
    )

    assert_invalid_api_key(response)
    assert upstream.call_count == 0


def test_missing_and_unknown_key_share_the_same_body(
    client: Any, chat: Any
) -> None:
    missing = client.post("/v1/chat/completions", json=chat())
    unknown = client.post(
        "/v1/chat/completions",
        json=chat(),
        headers={"Authorization": "Bearer " + "unknown-key-" + "0" * 48},
    )

    assert missing.json() == unknown.json()


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",
        "Bearer ",
        "Basic " + ALPHA_KEY,
        ALPHA_KEY,
        "Bearer " + ALPHA_KEY + " extra",
    ],
)
def test_malformed_authorization_header_is_rejected(
    client: Any, chat: Any, upstream: Any, header: str
) -> None:
    response = client.post(
        "/v1/chat/completions", json=chat(), headers={"Authorization": header}
    )

    assert_invalid_api_key(response)
    assert upstream.call_count == 0


def test_non_ascii_authorization_is_rejected_without_server_error(
    client: Any, chat: Any, upstream: Any
) -> None:
    response = client.post(
        "/v1/chat/completions",
        json=chat(),
        headers={"Authorization": "Bearer caf\u00e9".encode("utf-8")},
    )

    assert_invalid_api_key(response)
    assert upstream.call_count == 0


def test_bearer_scheme_is_case_insensitive(
    client: Any, chat: Any, upstream: Any
) -> None:
    response = client.post(
        "/v1/chat/completions",
        json=chat(),
        headers={"Authorization": f"bearer {ALPHA_KEY}"},
    )

    assert response.status_code == 200
    assert upstream.call_count == 1


def test_models_requires_authorization(client: Any) -> None:
    response = client.get("/v1/models")

    assert_invalid_api_key(response)
