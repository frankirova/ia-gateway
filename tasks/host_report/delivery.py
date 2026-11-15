"""WhatsApp delivery through the personal hub's authenticated ``POST /send``.

The hub owns the WAHA session, so this job never holds a WhatsApp credential: it
sends one compact message with a bearer token read from a file and a
deduplication key. The hub claims that key before calling WAHA, so a retried run
never sends the same report twice.

The message is deliberately short. A markdown report is larger than one WhatsApp
message, so the model output is condensed into the verdict plus its findings and
the deterministic totals stay next to it. Up to three measured facts — the
unreachable source, host findings, failing endpoints — ride between the totals
and the interpretation, translated from their values rather than narrated.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import time
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

__all__ = [
    "DEFAULT_CHAT",
    "DEFAULT_HUB_TIMEOUT_SECONDS",
    "DeliveryError",
    "HubSettings",
    "MEASURED_LINE_LIMIT",
    "MESSAGE_LIMIT",
    "NO_NEWS",
    "NO_LOG_NEWS",
    "UNREACHABLE_SENTENCES",
    "deduplication_key",
    "deliver",
    "render_message",
]

SEND_PATH = "/send"

#: The hub rejects a longer ``text``; the worst legal interpretation fits well
#: under this, so the limit only catches a bug or an unexpectedly long name.
MESSAGE_LIMIT = 4096
MAX_RESPONSE_BYTES = 65_536

DEFAULT_CHAT = "owner"
CHATS = ("owner", "business")
DEFAULT_HUB_TIMEOUT_SECONDS = 10.0

#: How a finding reads on a phone: the reply keeps English JSON keys and
#: severities, the message renders them in Spanish with a colour cue.
SEVERITY = {
    "high": ("🔴", "alta"),
    "medium": ("🟠", "media"),
    "low": ("🟡", "baja"),
}

NO_NEWS = "✅ Sin novedades en la ventana."
#: Said instead of :data:`NO_NEWS` when the model was skipped but measured facts
#: are on screen: a full disk is not "no news".
NO_LOG_NEWS = "🔎 Sin actividad en los logs; los avisos de arriba son lo que hay."
NOTHING_FOUND = "✅ Sin hallazgos que reportar."

#: At most three measured lines fit between the totals and the interpretation.
#: They are dropped from the end first when the message would not fit.
MEASURED_LINE_LIMIT = 3

#: Fixed Spanish sentences for a source the report could not reach. The remote's
#: own stderr is never part of them.
UNREACHABLE_SENTENCES = {
    "ssh_timeout": "⚠️ No pude relevar la VPS: no respondió por SSH",
    "ssh_failed": "⚠️ No pude relevar la VPS: falló la conexión SSH",
    "docker_unavailable": "⚠️ No pude relevar la VPS: no pude leer Docker",
    "invalid_bundle": "⚠️ No pude relevar la VPS: respuesta inválida",
    "too_large": "⚠️ No pude relevar la VPS: la respuesta fue demasiado grande",
    "config_invalid": (
        "⚠️ No pude relevar la VPS: la configuración de vps-readonly es inválida"
    ),
}

#: The collector only emits known codes, so this only guards a malformed digest.
UNKNOWN_UNREACHABLE = UNREACHABLE_SENTENCES["invalid_bundle"]

_KEY_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})")


@dataclass(frozen=True)
class HubSettings:
    """Everything needed for one delivery, resolved from the environment."""

    base_url: str | None = None
    token_file: str | None = None
    ca_bundle: str | None = None
    chat: str = DEFAULT_CHAT
    timeout_seconds: float = DEFAULT_HUB_TIMEOUT_SECONDS
    deduplication_key: str | None = None


class DeliveryError(RuntimeError):
    """A delivery failed. ``reason`` never contains a token or a raw body."""

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    """Refuse every redirect: the bearer token stays on the configured host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _count(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value == 1 else plural}"


def _totals(digest: dict[str, Any]) -> str:
    containers = digest.get("containers") or []
    lines = sum(int(entry.get("total_lines") or 0) for entry in containers)
    errors = sum(int(entry.get("error_lines") or 0) for entry in containers)
    restarts = sum(int(entry.get("restart_count") or 0) for entry in containers)
    return " · ".join(
        (
            _count(lines, "línea", "líneas"),
            _count(errors, "error", "errores"),
            _count(restarts, "reinicio", "reinicios"),
        )
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _whole(value: Any) -> int | None:
    """An integer percentage, or ``None`` when the value is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _endpoint_line(name: str, status: Any, reason: Any) -> str:
    if status is None:
        label = _text(reason) or "transport"
        return f"🔌 {name} no responde ({label})"
    return f"🔌 {name} respondió {status}"


def _finding_line(finding: dict[str, Any]) -> str | None:
    """One measured fact in Spanish, translated from ``kind`` and ``values``.

    The English ``message`` is never parsed: it is only the fallback for a kind
    this message does not know or malformed values.
    """
    kind = _text(finding.get("kind"))
    raw_values = finding.get("values")
    values = raw_values if isinstance(raw_values, dict) else {}

    if kind == "disk":
        mount = _text(values.get("mount"))
        percent = _whole(values.get("used_percent"))
        if mount and percent is not None:
            return f"💾 Disco {mount} al {percent}%"
    elif kind == "memory":
        percent = _whole(values.get("available_percent"))
        if percent is not None:
            return f"🧠 Memoria disponible {percent}%"
    elif kind == "load":
        load1 = _number(values.get("load1"))
        cpu_count = _whole(values.get("cpu_count"))
        if load1 is not None and cpu_count is not None:
            return f"⚙️ Carga {load1:.2f} con {cpu_count} CPU"
    elif kind == "endpoint":
        name = _text(values.get("name"))
        if name:
            return _endpoint_line(name, values.get("status"), values.get("reason"))
    elif kind == "service":
        name = _text(values.get("name"))
        if name:
            return f"⛔ Servicio sin correr: {name}"

    message = _text(finding.get("message")).strip()
    return f"⚠️ {message}" if message else None


def _measured_lines(digest: dict[str, Any]) -> list[str]:
    """The measured facts: unreachable source, host findings, failing endpoints.

    Endpoint findings arrive both as host findings and as probe results; an
    endpoint is rendered once, from whichever shape carries it.
    """
    unreachable: list[str] = []
    reason = digest.get("unreachable")
    if reason:
        unreachable.append(
            UNREACHABLE_SENTENCES.get(str(reason), UNKNOWN_UNREACHABLE)
        )

    findings: list[str] = []
    endpoints: list[str] = []
    named: set[str] = set()
    entries = digest.get("host_findings")
    if isinstance(entries, (list, tuple)):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            line = _finding_line(entry)
            if line is None:
                continue
            if _text(entry.get("kind")) == "endpoint":
                values = entry.get("values")
                name = _text(values.get("name")) if isinstance(values, dict) else ""
                if name:
                    named.add(name)
                endpoints.append(line)
            else:
                findings.append(line)

    probes = digest.get("probes")
    if isinstance(probes, (list, tuple)):
        for entry in probes:
            if not isinstance(entry, dict) or entry.get("ok", True):
                continue
            name = _text(entry.get("name"))
            if not name or name in named:
                continue
            named.add(name)
            endpoints.append(
                _endpoint_line(name, entry.get("status"), entry.get("reason"))
            )

    return (unreachable + findings + endpoints)[:MEASURED_LINE_LIMIT]


def _compose(
    digest: dict[str, Any],
    *,
    model_status: str,
    model_reason: str | None,
    interpretation: dict[str, Any] | None,
    measured: list[str],
) -> str:
    """The message with exactly the given measured lines."""
    window = digest.get("window") or {}
    lines = [
        f"🖥️ Servidor: {digest.get('host') or 'desconocido'} · "
        f"{window.get('duration') or 'ventana'}",
        f"📊 {_totals(digest)}",
    ]
    lines.extend(measured)
    lines.append("")
    if model_status == "skipped":
        lines.append(NO_LOG_NEWS if measured else NO_NEWS)
    elif interpretation is None or model_status != "used":
        reason = model_reason or "la respuesta del modelo no sirvió"
        lines.append(f"⚠️ Sin interpretación: {reason}.")
    else:
        summary = str(interpretation.get("summary") or "").strip()
        if summary:
            lines.extend([summary, ""])
        findings = interpretation.get("findings") or []
        if not findings:
            lines.append(NOTHING_FOUND)
        for index, finding in enumerate(findings, start=1):
            emoji, label = SEVERITY.get(
                str(finding["severity"]), ("⚪", str(finding["severity"]))
            )
            lines.append(
                f"{index}. {emoji} {finding['container']} ({label}): "
                f"{finding['what']}"
            )
            lines.append(f"   ↳ Por qué: {finding['why']}")
            lines.append(f"   ↳ Acción: {finding['action']}")
    return "\n".join(lines)


def render_message(
    digest: dict[str, Any],
    *,
    model_status: str,
    model_reason: str | None = None,
    interpretation: dict[str, Any] | None = None,
) -> str:
    """Render the compact message: header, totals, measured facts, findings.

    Measured lines are added after the totals and before the interpretation. If
    that pushes the message over the hub's limit they are dropped from the end
    first, so the interpretation is never the part that gives way.
    """
    measured = _measured_lines(digest)
    message = _compose(
        digest,
        model_status=model_status,
        model_reason=model_reason,
        interpretation=interpretation,
        measured=measured,
    )
    while len(message) > MESSAGE_LIMIT and measured:
        measured = measured[:-1]
        message = _compose(
            digest,
            model_status=model_status,
            model_reason=model_reason,
            interpretation=interpretation,
            measured=measured,
        )
    return message


def deduplication_key(
    digest: dict[str, Any], explicit: str | None = None
) -> str:
    """Key claimed by the hub: the explicit slot, else host plus window minute."""
    if explicit:
        return explicit
    window = digest.get("window") or {}
    until = str(window.get("until") or "")
    match = _KEY_STAMP.match(until)
    stamp = match.group(1) if match is not None else (until or "unknown")
    return f"host-report:{digest.get('host') or 'unknown'}:{stamp}"


def _read_token(token_file: str | None) -> str:
    if token_file is None:
        raise DeliveryError("the send token file is missing")
    try:
        with open(token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        raise DeliveryError("the send token file is missing") from None
    if not token:
        raise DeliveryError("the send token file is empty")
    return token


def _ssl_context(url: str, ca_bundle: str | None) -> ssl.SSLContext | None:
    """Verifying context for ``https``; ``None`` for a plain ``http`` URL."""
    if urlsplit(url).scheme.lower() != "https":
        return None
    if ca_bundle is None:
        raise DeliveryError("an https hub URL requires a CA bundle")
    return ssl.create_default_context(cafile=ca_bundle)


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        return f"hub returned HTTP {error.code}"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "hub request timed out"
    if isinstance(error, URLError):
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return "hub request timed out"
        return "hub connection failed"
    return "hub request failed"


def _result_of(raw: bytes) -> str:
    """``duplicate`` when the hub says it already had the key, else ``sent``."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "sent"
    if isinstance(document, dict) and document.get("status") == "duplicate":
        return "duplicate"
    return "sent"


def deliver(
    settings: HubSettings,
    text: str,
    key: str,
    *,
    diagnostics: Any = None,
) -> str:
    """Send exactly one message and return ``sent`` or ``duplicate``.

    Raises :class:`DeliveryError` for every failure. Nothing is retried, TLS
    verification is never relaxed and no token is ever printed.
    """
    if len(text) > MESSAGE_LIMIT:
        raise DeliveryError(
            f"the message is longer than the hub accepts ({len(text)} characters)"
        )
    if not key.strip():
        raise DeliveryError("the deduplication key is empty")
    if settings.base_url is None:
        raise DeliveryError("the hub URL is not configured")
    url = settings.base_url.rstrip("/") + SEND_PATH
    token = _read_token(settings.token_file)
    context = _ssl_context(url, settings.ca_bundle)

    body = json.dumps(
        {"chat": settings.chat, "text": text, "deduplication_key": key}
    ).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    handlers: list[Any] = [_NoRedirect()]
    if context is not None:
        handlers.append(HTTPSHandler(context=context))
    opener = build_opener(*handlers)

    status: int | None = None
    started = time.monotonic()
    try:
        with opener.open(request, timeout=settings.timeout_seconds) as response:
            status = getattr(response, "status", 200)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, OSError, HTTPException) as error:
        status = error.code if isinstance(error, HTTPError) else None
        reason = _failure_reason(error)
        if isinstance(error, HTTPError):
            error.close()
        raise DeliveryError(reason, status=status) from None
    finally:
        if diagnostics is not None:
            diagnostics(status, time.monotonic() - started)

    if len(raw) > MAX_RESPONSE_BYTES:
        raise DeliveryError("the hub response is too large")
    return _result_of(raw)
