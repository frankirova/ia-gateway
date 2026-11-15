"""Shared fixtures: temporary gateway configuration, app client, fake upstream."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from app import upstream as upstream_adapter
from app.main import create_app

UPSTREAM_BASE_URL = "http://upstream.test:8190"
UPSTREAM_MODEL = "test-model"

# Test-only credentials. They are deliberately obvious fakes and must never be
# reused as real application keys.
ALPHA_KEY = "alpha-test-key-0000000000000000000000000000000000000000000000"
BETA_KEY = "beta-test-key-0000000000000000000000000000000000000000000000000"

INVALID_API_KEY_MESSAGE = "Missing or invalid application credential."


def key_digest(key: str) -> str:
    """Return the lowercase SHA-256 hex digest of an application key."""
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def application_entry(
    application_id: str = "alpha",
    key: str = ALPHA_KEY,
    **overrides: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": application_id,
        "key_sha256": key_digest(key),
        "models": ["e4b"],
    }
    entry.update(overrides)
    return entry


def gateway_document(
    *,
    applications: list[dict[str, Any]] | None = None,
    upstream: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "upstream": {
            "base_url": UPSTREAM_BASE_URL,
            "model": UPSTREAM_MODEL,
            "connect_timeout": 1,
            "read_timeout": 5,
        },
        "applications": [application_entry()] if applications is None else applications,
    }
    if upstream is not None:
        document["upstream"].update(upstream)
    if limits is not None:
        document["limits"] = limits
    return document


def write_config(directory: Path, document: dict[str, Any]) -> Path:
    path = directory / "gateway.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def completion_bytes(
    content: str = "Generated answer.",
    finish_reason: str = "stop",
    usage: dict[str, int] | None = None,
) -> bytes:
    """Build an upstream chat-completion response body."""
    document: dict[str, Any] = {
        "id": "upstream-1",
        "object": "chat.completion",
        "created": 1,
        "model": UPSTREAM_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        document["usage"] = usage
    return json.dumps(document).encode("utf-8")


class FakeResponse:
    """Minimal stand-in for the response object returned by ``urlopen``."""

    def __init__(self, body: bytes = b"", status: int = 200) -> None:
        self._body = body
        self.status = status
        self._consumed = False

    def read(self, size: int = -1) -> bytes:
        if self._consumed:
            return b""
        self._consumed = True
        if size is None or size < 0:
            return self._body
        return self._body[:size]

    def close(self) -> None:
        return None

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class RecordingUpstream:
    """Fake ``urlopen`` that records calls and lets a test choose the outcome."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.handler: Callable[[Any, float | None], Any] | None = None

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        raw_body = getattr(request, "data", None)
        self.calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "timeout": timeout,
                "body": json.loads(raw_body) if raw_body else None,
                "headers": dict(request.header_items()),
            }
        )
        if self.handler is not None:
            return self.handler(request, timeout)
        return FakeResponse(completion_bytes())

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def bodies(self) -> list[dict[str, Any] | None]:
        return [call["body"] for call in self.calls]

    def respond(self, body: bytes, status: int = 200) -> None:
        self.handler = lambda _request, _timeout: FakeResponse(body, status)

    def fail(self, error: BaseException) -> None:
        def handler(_request: Any, _timeout: float | None) -> Any:
            raise error

        self.handler = handler


def chat_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "e4b",
        "messages": [{"role": "user", "content": "Hello there"}],
        "max_tokens": 32,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Write a configuration file and point ``GATEWAY_CONFIG`` at it."""

    def _configure(document: dict[str, Any] | None = None) -> Path:
        path = write_config(
            tmp_path, gateway_document() if document is None else document
        )
        monkeypatch.setenv("GATEWAY_CONFIG", str(path))
        return path

    return _configure


@pytest.fixture
def config_path(configure: Callable[..., Path]) -> Path:
    return configure()


@pytest.fixture
def app(config_path: Path) -> Any:
    return create_app()


@pytest.fixture
def client(app: Any):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def app_factory(configure: Callable[..., Path]) -> Callable[..., Any]:
    def _make(document: dict[str, Any] | None = None) -> Any:
        configure(document)
        return create_app()

    return _make


@pytest.fixture
def client_factory(app_factory: Callable[..., Any]):
    created: list[TestClient] = []

    def _make(document: dict[str, Any] | None = None) -> TestClient:
        test_client = TestClient(app_factory(document))
        test_client.__enter__()
        created.append(test_client)
        return test_client

    yield _make

    for test_client in created:
        test_client.__exit__(None, None, None)


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> RecordingUpstream:
    """Replace the adapter's ``urlopen`` with a recording fake."""
    recorder = RecordingUpstream()
    monkeypatch.setattr(upstream_adapter, "urlopen", recorder)
    return recorder


@pytest.fixture
def alpha_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ALPHA_KEY}"}


@pytest.fixture
def beta_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {BETA_KEY}"}


@pytest.fixture
def chat() -> Callable[..., dict[str, Any]]:
    return chat_payload


@pytest.fixture
def completions() -> Callable[..., bytes]:
    return completion_bytes
