"""Health endpoints: liveness never contacts the backend; readiness is cached."""

from __future__ import annotations

import urllib.error
from typing import Any

from conftest import UPSTREAM_BASE_URL, FakeResponse


def test_liveness_never_contacts_the_backend(client: Any, upstream: Any) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert upstream.call_count == 0


def test_readiness_reports_ok_when_the_backend_is_healthy(
    client: Any, upstream: Any
) -> None:
    upstream.handler = lambda _request, _timeout: FakeResponse(b"{}", status=200)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert upstream.call_count == 1
    assert upstream.calls[0]["url"] == f"{UPSTREAM_BASE_URL}/health"
    assert upstream.calls[0]["method"] == "GET"


def test_readiness_hides_backend_details_when_unavailable(
    client: Any, upstream: Any
) -> None:
    upstream.fail(urllib.error.URLError(OSError("connection refused")))

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "upstream" not in response.text.lower()
    assert UPSTREAM_BASE_URL not in response.text
    assert "refused" not in response.text


def test_readiness_failure_is_cached(client_factory: Any, upstream: Any) -> None:
    client = client_factory()
    upstream.fail(urllib.error.URLError(OSError("connection refused")))
    first = client.get("/health/ready")

    upstream.handler = None
    second = client.get("/health/ready")

    assert first.status_code == 503
    assert second.status_code == 503
    assert upstream.call_count == 1


def test_readiness_result_is_cached(client: Any, upstream: Any) -> None:
    upstream.handler = lambda _request, _timeout: FakeResponse(b"{}", status=200)

    first = client.get("/health/ready")
    second = client.get("/health/ready")

    assert first.status_code == 200
    assert second.status_code == 200
    assert upstream.call_count == 1


def test_readiness_never_performs_generation(client: Any, upstream: Any) -> None:
    client.get("/health/ready")

    assert upstream.call_count == 1
    assert all(call["url"].endswith("/health") for call in upstream.calls)
    assert upstream.bodies == [None]
