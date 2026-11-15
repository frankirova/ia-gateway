"""HTTP reachability probes measured from the host that runs the report.

One attempt per endpoint, no retries, no redirect following and no body read:
the report only needs to know whether each service answered, how fast, and which
fixed reason explains a failure. The response body is never treated as evidence.

Reasons come from a closed vocabulary so the report and its tests never depend on
free-form transport text.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from tasks.host_report.hoststate import HostFinding

__all__ = [
    "Endpoint",
    "ProbeConfigError",
    "ProbeResult",
    "parse_endpoints",
    "probe_all",
    "probe_endpoint",
    "probe_findings",
    "render_probes",
    "results_from_document",
]

REASONS = (
    "ok",
    "status_mismatch",
    "redirect",
    "timeout",
    "refused",
    "dns",
    "tls",
    "transport",
    "invalid_url",
)

DEFAULT_TIMEOUT_SECONDS = 5.0
_TRUNCATION = "…"


class ProbeConfigError(ValueError):
    """The endpoint configuration is unusable; the report must not guess."""


@dataclass(frozen=True)
class Endpoint:
    """One URL the report checks."""

    name: str
    url: str
    expect_status: int = 200
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ProbeResult:
    """What one attempt observed."""

    name: str
    url: str
    ok: bool
    status: int | None
    latency_ms: int | None
    reason: str


def _config_error(index: int, name: str | None, problem: str) -> ProbeConfigError:
    label = f"endpoint {index}"
    if name:
        label += f" ({name})"
    return ProbeConfigError(f"{label}: {problem}")


def parse_endpoints(raw: str | list | tuple | None) -> tuple[Endpoint, ...]:
    """Validate the configured endpoint list; never accept a partial list."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        if not raw.strip():
            return ()
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ProbeConfigError(f"endpoints are not valid JSON: {error.msg}") from error
    else:
        document = raw
    if not isinstance(document, (list, tuple)):
        raise ProbeConfigError("endpoints must be a JSON list")

    endpoints: list[Endpoint] = []
    seen: set[str] = set()
    for index, entry in enumerate(document, start=1):
        if not isinstance(entry, dict):
            raise _config_error(index, None, "must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _config_error(index, None, "name is required")
        name = name.strip()
        if name in seen:
            raise _config_error(index, name, "duplicate name")
        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            raise _config_error(index, name, "url is required")
        parts = urllib.parse.urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise _config_error(index, name, "url must be http or https with a host")
        expect_status = entry.get("expect_status", 200)
        if not isinstance(expect_status, int) or isinstance(expect_status, bool):
            raise _config_error(index, name, "expect_status must be an integer")
        timeout = entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise _config_error(index, name, "timeout_seconds must be greater than zero")
        seen.add(name)
        endpoints.append(
            Endpoint(
                name=name,
                url=url.strip(),
                expect_status=expect_status,
                timeout_seconds=float(timeout),
            )
        )
    return tuple(endpoints)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect: the report measures the configured URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _reason_for_exception(error: BaseException) -> str:
    if isinstance(error, urllib.error.URLError):
        return _reason_for_exception(error.reason)  # type: ignore[arg-type]
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, ConnectionRefusedError):
        return "refused"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ssl.SSLError):
        return "tls"
    return "transport"


def _result(endpoint: Endpoint, reason: str, status: int | None, latency_ms: int | None) -> ProbeResult:
    return ProbeResult(
        name=endpoint.name,
        url=endpoint.url,
        ok=reason == "ok",
        status=status,
        latency_ms=latency_ms,
        reason=reason,
    )


def probe_endpoint(endpoint: Endpoint) -> ProbeResult:
    """Perform exactly one attempt against ``endpoint``."""
    parts = urllib.parse.urlsplit(endpoint.url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return _result(endpoint, "invalid_url", None, None)

    request = urllib.request.Request(endpoint.url, method="GET")
    started = time.monotonic()
    try:
        response = _OPENER.open(request, timeout=endpoint.timeout_seconds)
    except urllib.error.HTTPError as error:
        elapsed = int((time.monotonic() - started) * 1000)
        try:
            error.close()
        except Exception:  # noqa: BLE001 - closing must never mask the verdict
            pass
        status = int(getattr(error, "code", 0) or 0) or None
        if status is not None and 300 <= status < 400:
            return _result(endpoint, "redirect", status, elapsed)
        return _result(endpoint, "status_mismatch", status, elapsed)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        return _result(endpoint, _reason_for_exception(error), None, None)

    elapsed = int((time.monotonic() - started) * 1000)
    try:
        status = int(response.status)
    finally:
        response.close()

    if status == endpoint.expect_status:
        return _result(endpoint, "ok", status, elapsed)
    return _result(endpoint, "status_mismatch", status, elapsed)


def probe_all(
    endpoints: tuple[Endpoint, ...] | list[Endpoint],
    *,
    timeout_seconds: float | None = None,
) -> tuple[ProbeResult, ...]:
    """Probe every endpoint in order; a failure never raises."""
    results: list[ProbeResult] = []
    for endpoint in endpoints:
        target = endpoint
        if timeout_seconds is not None:
            target = Endpoint(
                name=endpoint.name,
                url=endpoint.url,
                expect_status=endpoint.expect_status,
                timeout_seconds=float(timeout_seconds),
            )
        results.append(probe_endpoint(target))
    return tuple(results)


def probe_findings(results: tuple[ProbeResult, ...] | list[ProbeResult]) -> tuple[HostFinding, ...]:
    """Every endpoint that did not answer as expected, in input order."""
    findings: list[HostFinding] = []
    for result in results:
        if result.ok:
            continue
        detail = result.reason
        if result.status is not None:
            detail = f"{result.reason} {result.status}"
        findings.append(
            HostFinding(
                code=f"endpoint:{result.name}",
                severity="critical",
                message=f"endpoint {result.name} {detail}",
                kind="endpoint",
                values={
                    "name": result.name,
                    "reason": result.reason,
                    "status": result.status,
                },
            )
        )
    return tuple(findings)


def results_from_document(entries: object) -> tuple[ProbeResult, ...]:
    """Rebuild results from the digest, ignoring anything malformed."""
    if not isinstance(entries, (list, tuple)):
        return ()
    results: list[ProbeResult] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        status = entry.get("status")
        latency = entry.get("latency_ms")
        reason = entry.get("reason")
        url = entry.get("url")
        results.append(
            ProbeResult(
                name=name,
                url=url if isinstance(url, str) else "",
                ok=bool(entry.get("ok")),
                status=status if isinstance(status, int) and not isinstance(status, bool) else None,
                latency_ms=latency if isinstance(latency, int) and not isinstance(latency, bool) else None,
                reason=reason if isinstance(reason, str) and reason in REASONS else "transport",
            )
        )
    return tuple(results)


def _format_result(result: ProbeResult) -> str:
    if result.ok:
        latency = "" if result.latency_ms is None else f" {result.latency_ms}ms"
        return f"{result.name} {result.status}{latency}"
    if result.status is not None:
        return f"{result.name} {result.reason} {result.status}"
    return f"{result.name} {result.reason}"


def render_probes(
    results: tuple[ProbeResult, ...] | list[ProbeResult], *, limit_chars: int
) -> str:
    """Render one bounded line per endpoint, never splitting a line mid-number."""
    if limit_chars <= 0:
        return ""
    rendered: list[str] = []
    used = 0
    for result in results:
        line = _format_result(result)
        cost = len(line) + (1 if rendered else 0)
        if used + cost > limit_chars:
            break
        rendered.append(line)
        used += cost
    text = "\n".join(rendered)
    if len(text) > limit_chars:
        text = text[: max(0, limit_chars - 1)] + _TRUNCATION
    return text
