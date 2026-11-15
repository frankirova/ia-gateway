"""Read a remote host's evidence bundle over one SSH call.

The remote side runs a fixed read-only program behind ``restrict,command=`` and
prints one JSON document: host state plus, per running container, the listing
entry, the inspect document and the log text with timestamps. This module turns
that document into the same surface the local collector already consumes, so the
report pipeline is unchanged.

One call per run, no client-supplied argument, no shell, and a bounded read.
"""

from __future__ import annotations

import io
import json
import os
import re
import select
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tasks.host_report.docker_api import decode_log_line

__all__ = [
    "BUNDLE_VERSION",
    "Bundle",
    "BundleContainer",
    "BundleDockerClient",
    "BundleError",
    "BundleUnavailable",
    "fetch_bundle",
    "remote_sentence",
    "render_remote_error",
]

BUNDLE_VERSION = 1

#: Ceiling for one bundle read; the remote program caps at 8 MiB by itself.
DEFAULT_MAX_BYTES = 16 * 1024 * 1024

#: The only reason vocabulary a report may print for an unreachable host.
REMOTE_REASONS = (
    "ssh_failed",
    "ssh_timeout",
    "invalid_bundle",
    "docker_unavailable",
    "too_large",
    "config_invalid",
)

_READ_CHUNK = 64 * 1024
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z$")


class BundleUnavailable(Exception):
    """The remote host did not produce a usable bundle; the report must say so."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason if reason in REMOTE_REASONS else "invalid_bundle"
        self.detail = detail
        super().__init__(self.reason)


class BundleError(RuntimeError):
    """The bundle is usable but a requested container is not in it."""


@dataclass(frozen=True)
class BundleContainer:
    """One container as the remote host described it."""

    listing: dict[str, Any]
    inspect: dict[str, Any]
    logs: bytes
    error: str | None = None


@dataclass(frozen=True)
class Bundle:
    """The whole remote evidence bundle."""

    containers: tuple[BundleContainer, ...]
    host: dict[str, Any]
    collected_at: str
    truncated: bool = False
    #: Configured on the remote host but not running there. The remote program
    #: owns the list; the report never invents one.
    selection_missing: tuple[str, ...] = ()


def _ssh_argv(
    ssh_binary: str, ssh_target: str, remote_command: str, timeout_seconds: float
) -> list[str]:
    connect_timeout = max(1, int(timeout_seconds))
    return [
        ssh_binary,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "StrictHostKeyChecking=yes",
        ssh_target,
        remote_command,
    ]


def _read_bounded(proc: subprocess.Popen, deadline: float, limit: int) -> bytes:
    """Read stdout until EOF, the deadline or the ceiling; never blocks past it."""
    stream = proc.stdout
    if stream is None:  # pragma: no cover - Popen always gets a pipe here
        raise BundleUnavailable("invalid_bundle")
    descriptor = stream.fileno()
    buffer = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BundleUnavailable("ssh_timeout")
        ready, _, _ = select.select([descriptor], [], [], remaining)
        if not ready:
            raise BundleUnavailable("ssh_timeout")
        try:
            chunk = os.read(descriptor, _READ_CHUNK)
        except OSError as error:
            raise BundleUnavailable("ssh_failed", type(error).__name__) from error
        if not chunk:
            return bytes(buffer)
        buffer += chunk
        if len(buffer) > limit:
            raise BundleUnavailable("too_large")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - kill already sent
        pass


def _selection_missing(value: object) -> tuple[str, ...]:
    """Configured containers the remote host reported as not running.

    The key is advisory: a bundle without it, or with a shape this version does
    not understand, simply carries no missing services. A report must never be
    lost over an optional section.
    """
    if not isinstance(value, dict):
        return ()
    missing = value.get("missing")
    if not isinstance(missing, list):
        return ()
    return tuple(name for name in missing if isinstance(name, str) and name)


def _parse_bundle(payload: bytes) -> Bundle:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BundleUnavailable("invalid_bundle", "not utf-8") from error
    if not text.strip():
        raise BundleUnavailable("invalid_bundle", "empty")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise BundleUnavailable("invalid_bundle", "not json") from error
    if not isinstance(document, dict):
        raise BundleUnavailable("invalid_bundle", "not an object")

    error_code = document.get("error")
    if error_code == "docker_unavailable":
        raise BundleUnavailable("docker_unavailable")
    if error_code == "config_invalid":
        raise BundleUnavailable("config_invalid")
    if document.get("version") != BUNDLE_VERSION:
        raise BundleUnavailable("invalid_bundle", "version")
    if error_code is not None:
        raise BundleUnavailable("invalid_bundle", "remote error")

    entries = document.get("containers")
    if not isinstance(entries, list):
        raise BundleUnavailable("invalid_bundle", "containers")

    containers: list[BundleContainer] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise BundleUnavailable("invalid_bundle", "container entry")
        listing = entry.get("listing")
        inspect = entry.get("inspect")
        logs = entry.get("logs")
        if not isinstance(listing, dict) or not isinstance(inspect, dict):
            raise BundleUnavailable("invalid_bundle", "container document")
        if not isinstance(logs, str):
            logs = ""
        error = entry.get("error")
        containers.append(
            BundleContainer(
                listing=listing,
                inspect=inspect,
                logs=logs.encode("utf-8"),
                error=error if isinstance(error, str) else None,
            )
        )

    host = document.get("host")
    if not isinstance(host, dict):
        host = {}
    collected_at = document.get("collected_at")
    if not isinstance(collected_at, str):
        collected_at = ""
    truncated = bool(document.get("truncated"))
    return Bundle(
        containers=tuple(containers),
        host=host,
        collected_at=collected_at,
        truncated=truncated,
        selection_missing=_selection_missing(document.get("selection")),
    )


def fetch_bundle(
    *,
    ssh_target: str,
    timeout_seconds: float,
    ssh_binary: str = "ssh",
    remote_command: str = "vps-readonly",
    max_bytes: int | None = None,
) -> Bundle:
    """Run exactly one SSH call and return the parsed bundle."""
    if not ssh_target or not ssh_target.strip():
        raise BundleUnavailable("ssh_failed", "no target")
    if timeout_seconds <= 0:
        raise BundleUnavailable("ssh_timeout", "bad timeout")

    argv = _ssh_argv(ssh_binary, ssh_target.strip(), remote_command, timeout_seconds)
    limit = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    deadline = time.monotonic() + timeout_seconds
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise BundleUnavailable("ssh_failed", type(error).__name__) from error

    try:
        payload = _read_bounded(proc, deadline, limit)
        try:
            returncode = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise BundleUnavailable("ssh_timeout") from error
    except BundleUnavailable:
        _stop(proc)
        raise
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    if returncode != 0 and not payload:
        raise BundleUnavailable("ssh_failed", f"exit {returncode}")
    return _parse_bundle(payload)


def remote_sentence(reason: str) -> str:
    """One fixed English sentence for the report; never the remote's stderr."""
    labels = {
        "ssh_failed": "ssh failed",
        "ssh_timeout": "ssh timeout",
        "invalid_bundle": "invalid bundle",
        "docker_unavailable": "docker unavailable",
        "config_invalid": "remote config invalid",
        "too_large": "bundle too large",
    }
    return f"VPS unreachable: {labels.get(reason, 'ssh failed')}"


def render_remote_error(exc: BaseException) -> str:
    """The sentence for an unreachable host, from any exception that carries one."""
    return remote_sentence(str(getattr(exc, "reason", "invalid_bundle")))


def _line_epoch(line: str) -> float | None:
    head = line.split(" ", 1)[0]
    match = _TIMESTAMP.match(head)
    if match is None:
        return None
    try:
        moment = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc).timestamp()


class BundleDockerClient:
    """The collector's Docker surface, served from an already fetched bundle."""

    def __init__(self, bundle: Bundle) -> None:
        self.bundle = bundle
        self._by_id = {str(item.listing.get("Id")): item for item in bundle.containers}

    def list_containers(self) -> list[dict[str, Any]]:
        return [dict(item.listing) for item in self.bundle.containers]

    def inspect_container(self, container_id: str) -> dict[str, Any]:
        container = self._by_id.get(str(container_id))
        if container is None:
            raise BundleUnavailable("invalid_bundle", "unknown container")
        return dict(container.inspect)

    def open_logs(self, container_id: str, *, since: int = 0, tail: int | None = None) -> io.BytesIO:
        container = self._by_id.get(str(container_id))
        if container is None:
            raise BundleUnavailable("invalid_bundle", "unknown container")
        return io.BytesIO(container.logs)

    def read_log_lines(
        self,
        container_id: str,
        *,
        since: int,
        tail: int,
        max_lines: int,
    ) -> tuple[list[str], bool]:
        stream = self.open_logs(container_id, since=since, tail=tail)
        lines: list[str] = []
        truncated = False
        for raw in stream.read().split(b"\n"):
            if not raw:
                continue
            line = decode_log_line(raw)
            epoch = _line_epoch(line)
            if epoch is not None and epoch < since:
                continue
            if len(lines) >= max_lines:
                truncated = True
                break
            lines.append(line)
        return lines, truncated

    def list_host_state(self) -> dict[str, Any]:
        return dict(self.bundle.host)
