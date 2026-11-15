"""Tests for the HTTP probe: configuration, reasons, findings and rendering."""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tasks.host_report.probe import (
    Endpoint,
    ProbeConfigError,
    ProbeResult,
    parse_endpoints,
    probe_all,
    probe_endpoint,
    probe_findings,
    render_probes,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Handler(BaseHTTPRequestHandler):
    """Answer according to the path: /ok, /status/<n>, /redirect, /slow, /unread."""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/status/"):
            code = int(self.path.rsplit("/", 1)[1])
            self.send_response(code)
            self.end_headers()
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(0.5)
            self.send_response(200)
            self.end_headers()
            return
        if self.path == "/unread":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _endpoint(url: str, **overrides) -> Endpoint:
    document = {"name": "api", "url": url}
    document.update(overrides)
    return parse_endpoints([document])[0]


def test_parse_returns_nothing_for_absent_configuration():
    assert parse_endpoints(None) == ()
    assert parse_endpoints("") == ()
    assert parse_endpoints([]) == ()


def test_parse_applies_defaults():
    (endpoint,) = parse_endpoints('[{"name": "api", "url": "https://api.example/health"}]')

    assert endpoint == Endpoint(
        name="api",
        url="https://api.example/health",
        expect_status=200,
        timeout_seconds=5.0,
    )


def test_parse_accepts_an_already_parsed_list():
    assert parse_endpoints([{"name": "a", "url": "http://a.test"}])[0].name == "a"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"name": "a"}', "endpoints must be a JSON list"),
        ('["api"]', "endpoint 1: must be an object"),
        ('[{"url": "http://a.test"}]', "endpoint 1: name is required"),
        ('[{"name": " ", "url": "http://a.test"}]', "endpoint 1: name is required"),
        ('[{"name": "a"}]', "endpoint 1 (a): url is required"),
        (
            '[{"name": "a", "url": "ftp://a.test"}]',
            "endpoint 1 (a): url must be http or https with a host",
        ),
        ('[{"name": "a", "url": "http:///x"}]', "endpoint 1 (a): url must be http or https with a host"),
        (
            '[{"name": "a", "url": "http://a.test", "expect_status": true}]',
            "endpoint 1 (a): expect_status must be an integer",
        ),
        (
            '[{"name": "a", "url": "http://a.test", "timeout_seconds": 0}]',
            "endpoint 1 (a): timeout_seconds must be greater than zero",
        ),
        (
            '[{"name": "a", "url": "http://a.test"}, {"name": "a", "url": "http://b.test"}]',
            "endpoint 2 (a): duplicate name",
        ),
    ],
)
def test_parse_rejects_unusable_configuration(raw, message):
    with pytest.raises(ProbeConfigError) as caught:
        parse_endpoints(raw)

    assert str(caught.value) == message


def test_parse_rejects_invalid_json_with_its_own_message():
    with pytest.raises(ProbeConfigError) as caught:
        parse_endpoints("{")

    assert str(caught.value).startswith("endpoints are not valid JSON: ")


def test_probe_reports_ok_with_status_and_latency(server):
    result = probe_endpoint(_endpoint(f"{server}/ok"))

    assert result.ok is True
    assert result.reason == "ok"
    assert result.status == 200
    assert isinstance(result.latency_ms, int)
    assert result.latency_ms >= 0


def test_probe_compares_against_the_expected_status(server):
    result = probe_endpoint(_endpoint(f"{server}/status/204", expect_status=200))

    assert result.ok is False
    assert result.reason == "status_mismatch"
    assert result.status == 204


def test_probe_accepts_a_matching_non_200_status(server):
    result = probe_endpoint(_endpoint(f"{server}/status/204", expect_status=204))

    assert (result.ok, result.reason, result.status) == (True, "ok", 204)


def test_probe_never_follows_a_redirect(server):
    result = probe_endpoint(_endpoint(f"{server}/redirect"))

    assert (result.ok, result.reason, result.status) == (False, "redirect", 302)


def test_probe_reports_a_server_error_status(server):
    result = probe_endpoint(_endpoint(f"{server}/status/502"))

    assert (result.ok, result.reason, result.status) == (False, "status_mismatch", 502)


def test_probe_reports_a_refused_connection():
    result = probe_endpoint(_endpoint(f"http://127.0.0.1:{_free_port()}/ok"))

    assert (result.ok, result.reason, result.status) == (False, "refused", None)


def test_probe_reports_a_timeout(server):
    result = probe_endpoint(
        _endpoint(f"{server}/slow", timeout_seconds=0.05)
    )

    assert (result.ok, result.reason, result.status) == (False, "timeout", None)


def test_probe_reports_a_dns_failure():
    result = probe_endpoint(_endpoint("http://reports.invalid/ok"))

    assert (result.ok, result.reason, result.status) == (False, "dns", None)


def test_probe_reports_an_invalid_url_without_attempting():
    result = probe_endpoint(Endpoint(name="api", url="ftp://a.test"))

    assert (result.ok, result.reason, result.status) == (False, "invalid_url", None)


def test_probe_does_not_read_the_body(server):
    result = probe_endpoint(_endpoint(f"{server}/unread"))

    assert (result.ok, result.status) == (True, 200)


def test_probe_all_preserves_order_and_overrides_the_timeout(server):
    endpoints = (
        _endpoint(f"{server}/ok", timeout_seconds=0.05),
        Endpoint(name="slow", url=f"{server}/slow", timeout_seconds=5.0),
    )

    results = probe_all(endpoints, timeout_seconds=0.05)

    assert [result.name for result in results] == ["api", "slow"]
    assert results[1].reason == "timeout"


def test_probe_findings_only_names_failures_with_their_status():
    results = (
        ProbeResult("api", "http://a.test", True, 200, 5, "ok"),
        ProbeResult("web", "http://b.test", False, 502, 9, "status_mismatch"),
        ProbeResult("db", "http://c.test", False, None, None, "timeout"),
    )

    findings = probe_findings(results)

    assert [(item.code, item.severity) for item in findings] == [
        ("endpoint:web", "critical"),
        ("endpoint:db", "critical"),
    ]
    assert [item.message for item in findings] == [
        "endpoint web status_mismatch 502",
        "endpoint db timeout",
    ]


def test_render_probes_is_one_line_per_result():
    results = (
        ProbeResult("api", "http://a.test", True, 200, 42, "ok"),
        ProbeResult("web", "http://b.test", False, 302, 7, "redirect"),
        ProbeResult("db", "http://c.test", False, None, None, "timeout"),
    )

    assert render_probes(results, limit_chars=200).splitlines() == [
        "api 200 42ms",
        "web redirect 302",
        "db timeout",
    ]


def test_render_probes_respects_the_character_cap():
    results = (
        ProbeResult("api", "http://a.test", True, 200, 42, "ok"),
        ProbeResult("web", "http://b.test", False, 302, 7, "redirect"),
    )

    text = render_probes(results, limit_chars=12)

    assert len(text) <= 12
    assert text == "api 200 42ms"


def test_render_probes_with_zero_budget_is_empty():
    assert render_probes((), limit_chars=0) == ""
