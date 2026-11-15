"""Request validation, model resolution and the error envelope."""

from __future__ import annotations

from typing import Any, Callable

import pytest

from conftest import application_entry, gateway_document


def assert_error(
    response: Any,
    *,
    status: int,
    code: str,
    param: str | None,
    message: str | None = None,
) -> None:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"message", "type", "param", "code"}
    assert error["code"] == code
    assert error["param"] == param
    assert isinstance(error["message"], str) and error["message"]
    if message is not None:
        assert error["message"] == message
    assert response.headers["X-Request-ID"]


INVALID_FIELD_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("unknown top-level field", {"top_p": 0.9}, "top_p"),
    ("model has wrong type", {"model": 42}, "model"),
    ("model is empty", {"model": ""}, "model"),
    ("messages is empty", {"messages": []}, "messages"),
    ("messages is not a list", {"messages": "hello"}, "messages"),
    ("message is not an object", {"messages": ["hello"]}, "messages[0]"),
    (
        "message has an extra key",
        {"messages": [{"role": "user", "content": "hi", "name": "x"}]},
        "messages[0]",
    ),
    (
        "role is unsupported",
        {"messages": [{"role": "tool", "content": "hi"}]},
        "messages[0].role",
    ),
    (
        "role is missing",
        {"messages": [{"content": "hi"}]},
        "messages[0].role",
    ),
    (
        "content is empty",
        {"messages": [{"role": "user", "content": ""}]},
        "messages[0].content",
    ),
    (
        "content has the wrong type",
        {"messages": [{"role": "user", "content": 7}]},
        "messages[0].content",
    ),
    (
        "content is missing",
        {"messages": [{"role": "user"}]},
        "messages[0].content",
    ),
    ("max_tokens is a boolean", {"max_tokens": True}, "max_tokens"),
    ("max_tokens is zero", {"max_tokens": 0}, "max_tokens"),
    ("max_tokens is negative", {"max_tokens": -1}, "max_tokens"),
    ("max_tokens is a float", {"max_tokens": 1.5}, "max_tokens"),
    ("max_tokens is a string", {"max_tokens": "32"}, "max_tokens"),
    ("max_tokens exceeds the application limit", {"max_tokens": 513}, "max_tokens"),
    ("temperature is below the range", {"temperature": -0.1}, "temperature"),
    ("temperature is above the range", {"temperature": 1.1}, "temperature"),
    ("temperature is a string", {"temperature": "0.5"}, "temperature"),
    ("temperature is a boolean", {"temperature": True}, "temperature"),
    ("temperature is null", {"temperature": None}, "temperature"),
    ("temperature is an array", {"temperature": [0.5]}, "temperature"),
    ("reasoning is a string", {"reasoning": "false"}, "reasoning"),
    ("reasoning is a number", {"reasoning": 0}, "reasoning"),
    ("reasoning is null", {"reasoning": None}, "reasoning"),
    ("stream is enabled", {"stream": True}, "stream"),
    ("stream is not exactly false", {"stream": "false"}, "stream"),
]


@pytest.mark.parametrize(
    ("label", "overrides", "param"),
    INVALID_FIELD_CASES,
    ids=[case[0] for case in INVALID_FIELD_CASES],
)
def test_field_level_validation_rejects_with_envelope(
    client: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
    label: str,
    overrides: dict[str, Any],
    param: str,
) -> None:
    response = client.post(
        "/v1/chat/completions",
        json=chat(**overrides),
        headers=alpha_headers,
    )

    assert_error(response, status=400, code="invalid_request", param=param)
    assert upstream.call_count == 0


@pytest.mark.parametrize("missing_field", ["model", "messages"])
def test_required_fields_are_enforced(
    client: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
    missing_field: str,
) -> None:
    payload = chat()
    payload.pop(missing_field)

    response = client.post(
        "/v1/chat/completions", json=payload, headers=alpha_headers
    )

    assert_error(
        response, status=400, code="invalid_request", param=missing_field
    )
    assert upstream.call_count == 0


@pytest.mark.parametrize(
    "raw_body",
    [b"{not json", b"", b"[1, 2, 3]", b'{"model": NaN}'],
)
def test_malformed_json_is_rejected(
    client: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
    raw_body: bytes,
) -> None:
    response = client.post(
        "/v1/chat/completions",
        content=raw_body,
        headers={**alpha_headers, "Content-Type": "application/json"},
    )

    assert_error(response, status=400, code="invalid_request", param=None)
    assert upstream.call_count == 0


def test_model_not_allowed_for_application(
    client_factory: Callable[..., Any],
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
) -> None:
    client = client_factory(
        gateway_document(applications=[application_entry(models=["other-model"])])
    )

    response = client.post(
        "/v1/chat/completions",
        json=chat(model="e4b"),
        headers=alpha_headers,
    )

    assert_error(response, status=403, code="model_not_allowed", param=None)
    assert upstream.call_count == 0


def test_unknown_alias_for_application_that_lists_it(
    client_factory: Callable[..., Any],
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
) -> None:
    client = client_factory(
        gateway_document(
            applications=[application_entry(models=["e4b", "not-configured"])]
        )
    )

    response = client.post(
        "/v1/chat/completions",
        json=chat(model="not-configured"),
        headers=alpha_headers,
    )

    assert_error(response, status=400, code="invalid_request", param="model")
    assert upstream.call_count == 0


def test_unknown_alias_does_not_consume_an_inference_slot(
    client_factory: Callable[..., Any],
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
) -> None:
    client = client_factory(
        gateway_document(
            applications=[application_entry(models=["e4b", "not-configured"])]
        )
    )

    rejected = client.post(
        "/v1/chat/completions",
        json=chat(model="not-configured"),
        headers=alpha_headers,
    )
    accepted = client.post("/v1/chat/completions", json=chat(), headers=alpha_headers)

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert upstream.call_count == 1
