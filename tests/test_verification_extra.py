"""Regression tests for defects proved during independent verification."""

from __future__ import annotations

import hashlib
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import upstream as upstream_adapter
from app.config import UpstreamConfig, load_config
from conftest import ALPHA_KEY, application_entry, gateway_document, write_config


def test_upstream_redirect_is_not_followed():
    """A backend redirect must not re-send the credential to another target."""
    seen: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def _handle(self) -> None:
            seen.append((self.command, self.path))
            if self.path == "/v1/chat/completions":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        upstream = UpstreamConfig(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            model="m",
            connect_timeout=1.0,
            read_timeout=5.0,
            api_key="backend-secret",
        )
        with pytest.raises(upstream_adapter.UpstreamError):
            upstream_adapter.chat_completion(
                upstream, "m", [{"role": "user", "content": "hi"}], 8
            )
    finally:
        server.shutdown()

    assert seen == [("POST", "/v1/chat/completions")]


def test_unknown_alias_is_invalid_request(client, chat, upstream, alpha_headers):
    """An alias the gateway does not define is invalid input, not a permission error."""
    response = client.post(
        "/v1/chat/completions", json=chat(model="gpt-4"), headers=alpha_headers
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert upstream.call_count == 0


def test_config_rejects_digest_with_trailing_newline(monkeypatch, tmp_path):
    """A malformed digest must fail closed instead of creating a dead application."""
    digest = hashlib.sha256(ALPHA_KEY.encode()).hexdigest() + "\n"
    path = write_config(
        tmp_path, gateway_document(applications=[application_entry(key_sha256=digest)])
    )
    monkeypatch.setenv("GATEWAY_CONFIG", str(path))

    with pytest.raises(ValueError):
        load_config()


def test_rejected_alias_is_not_written_to_logs(client, chat, alpha_headers, caplog):
    """Only a validated alias may reach the request log."""
    caplog.set_level(logging.INFO)

    client.post(
        "/v1/chat/completions", json=chat(model="leaky-unvalidated-value"), headers=alpha_headers
    )

    assert "leaky-unvalidated-value" not in caplog.text
