"""Structured request logging without prompts, completions or credentials."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from conftest import ALPHA_KEY, completion_bytes

LOGGER_NAME = "gateway"


def last_log_payload(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    records = [
        record for record in caplog.records if record.name == LOGGER_NAME
    ]
    assert records, "expected at least one gateway log record"
    return json.loads(records[-1].getMessage())


def test_successful_request_writes_one_structured_line(
    client: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    upstream.respond(
        completion_bytes(
            usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
        )
    )

    response = client.post(
        "/v1/chat/completions", json=chat(), headers=alpha_headers
    )
    payload = last_log_payload(caplog)

    assert response.status_code == 200
    assert len([r for r in caplog.records if r.name == LOGGER_NAME]) == 1
    assert payload["request_id"] == response.headers["X-Request-ID"]
    assert payload["application_id"] == "alpha"
    assert payload["model"] == "e4b"
    assert payload["status"] == 200
    assert payload["code"] is None
    assert isinstance(payload["duration_ms"], (int, float))
    assert payload["duration_ms"] >= 0
    assert payload["prompt_tokens"] == 12
    assert payload["completion_tokens"] == 4
    assert payload["total_tokens"] == 16


def test_rejected_request_logs_the_error_code(
    client: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    response = client.post(
        "/v1/chat/completions",
        json=chat(model="not-allowed"),
        headers=alpha_headers,
    )
    payload = last_log_payload(caplog)

    assert response.status_code == 400
    assert payload["status"] == 400
    assert payload["code"] == "invalid_request"
    assert payload["application_id"] == "alpha"
    assert payload["prompt_tokens"] is None


def test_unauthenticated_request_is_logged_without_application_id(
    client: Any,
    chat: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    response = client.post("/v1/chat/completions", json=chat())
    payload = last_log_payload(caplog)

    assert response.status_code == 401
    assert payload["application_id"] is None
    assert payload["status"] == 401
    assert payload["code"] == "invalid_api_key"


def test_logs_never_contain_message_text_completion_or_keys(
    client: Any,
    chat: Any,
    upstream: Any,
    alpha_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    prompt_text = "SECRET-PROMPT-TEXT-9f3a"
    completion_text = "SECRET-COMPLETION-TEXT-7b1c"
    upstream.respond(completion_bytes(content=completion_text))

    response = client.post(
        "/v1/chat/completions",
        json=chat(messages=[{"role": "user", "content": prompt_text}]),
        headers=alpha_headers,
    )

    assert response.status_code == 200
    assert prompt_text not in caplog.text
    assert completion_text not in caplog.text
    assert ALPHA_KEY not in caplog.text
    assert ALPHA_KEY not in response.text
