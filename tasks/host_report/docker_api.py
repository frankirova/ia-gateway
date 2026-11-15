"""Minimal Docker Engine HTTP API client over a unix socket or TCP.

Only the three read-only endpoints the collector needs are implemented. Every
call has a timeout and is never retried. Log bodies are consumed as a stream so
the caller can stop early once its line budget is exhausted.
"""

from __future__ import annotations

import http.client
import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

__all__ = [
    "DEFAULT_DOCKER_URL",
    "DockerClient",
    "DockerError",
    "DockerUnavailable",
    "LogStreamReader",
    "decode_log_line",
    "parse_docker_url",
]

DEFAULT_DOCKER_URL = "unix:///var/run/docker.sock"
DEFAULT_SOCKET_PATH = "/var/run/docker.sock"

#: Largest single JSON body the client will read (container inspect and listing).
MAX_JSON_BODY = 8 * 1024 * 1024
#: Docker multiplexed framing is 8 bytes of header plus an up to 4 GiB payload;
#: anything larger than this is treated as a plain (non-multiplexed) body.
MAX_FRAME_PAYLOAD = 16 * 1024 * 1024
#: Chunk size for streaming a log body.
READ_CHUNK = 64 * 1024

_LOG_HEADER_SIZE = 8


class DockerError(RuntimeError):
    """A Docker API call failed (non-200 status, transport error, bad payload)."""


class DockerUnavailable(DockerError):
    """The Docker endpoint could not be reached at all."""


@dataclass(frozen=True)
class DockerEndpoint:
    """A parsed Docker base URL."""

    scheme: str  # "unix" or "http"
    socket_path: str | None = None
    host: str | None = None
    port: int | None = None
    path_prefix: str = ""

    @property
    def display(self) -> str:
        if self.scheme == "unix":
            return f"unix://{self.socket_path}"
        return f"http://{self.host}:{self.port}{self.path_prefix}"


def parse_docker_url(value: str) -> DockerEndpoint:
    """Parse ``unix:///path/to.sock`` or ``http://host:port[/prefix]``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("docker URL must not be empty")
    text = value.strip()
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme == "unix":
        if parts.netloc:
            raise ValueError(
                "unix docker URL must be written as unix:///absolute/path.sock"
            )
        socket_path = parts.path or DEFAULT_SOCKET_PATH
        if not socket_path.startswith("/"):
            raise ValueError("unix docker URL must contain an absolute socket path")
        return DockerEndpoint(scheme="unix", socket_path=socket_path)
    if scheme == "http":
        host = parts.hostname
        if not host:
            raise ValueError("http docker URL must contain a host")
        port = parts.port or 80
        return DockerEndpoint(
            scheme="http",
            host=host,
            port=port,
            path_prefix=parts.path.rstrip("/"),
        )
    raise ValueError(
        f"unsupported docker URL scheme {scheme!r}: use unix:// or http://"
    )


class _UnixHTTPConnection(http.client.HTTPConnection):
    """``HTTPConnection`` that dials a UNIX domain socket."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:  # noqa: D401 - http.client hook
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _looks_like_frame_header(buffer: bytes) -> bool:
    if len(buffer) < _LOG_HEADER_SIZE:
        return False
    if buffer[0] not in (0, 1, 2):
        return False
    if buffer[1:4] != b"\x00\x00\x00":
        return False
    return int.from_bytes(buffer[4:8], "big") <= MAX_FRAME_PAYLOAD


class _PayloadSplitter:
    """Split a Docker log body into payloads, multiplexed or plain.

    Non-TTY containers stream an 8-byte frame header (stream type, three zero
    bytes, big-endian payload length) before each payload. A body that does not
    start with a plausible header is passed through unchanged.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._mode: str | None = None
        self._decided = False

    def feed(self, chunk: bytes) -> list[bytes]:
        payloads: list[bytes] = []
        self._buffer += chunk
        while True:
            if not self._decided:
                if not self._buffer:
                    return payloads
                if self._buffer[0] not in (0, 1, 2):
                    self._mode = "plain"
                    self._decided = True
                elif len(self._buffer) < _LOG_HEADER_SIZE:
                    return payloads
                elif _looks_like_frame_header(bytes(self._buffer)):
                    self._mode = "multiplexed"
                    self._decided = True
                else:
                    self._mode = "plain"
                    self._decided = True
            if self._mode == "plain":
                payloads.append(bytes(self._buffer))
                self._buffer.clear()
                return payloads
            if len(self._buffer) < _LOG_HEADER_SIZE:
                return payloads
            length = int.from_bytes(self._buffer[4:8], "big")
            if len(self._buffer) < _LOG_HEADER_SIZE + length:
                return payloads
            payload = bytes(self._buffer[_LOG_HEADER_SIZE : _LOG_HEADER_SIZE + length])
            del self._buffer[: _LOG_HEADER_SIZE + length]
            if payload:
                payloads.append(payload)

    def flush(self) -> bytes:
        """Return whatever is left, tolerating a truncated final frame."""
        remainder = bytes(self._buffer)
        self._buffer.clear()
        if not remainder:
            return b""
        if self._mode == "multiplexed" and len(remainder) >= _LOG_HEADER_SIZE:
            if _looks_like_frame_header(remainder):
                return remainder[_LOG_HEADER_SIZE:]
            return b""
        return remainder


def decode_log_line(raw: bytes) -> str:
    """Decode one raw log line into text, never raising on bad bytes."""
    return raw.decode("utf-8", errors="replace").rstrip("\r")


class LogStreamReader:
    """Consume a log response and yield at most ``max_lines`` decoded lines."""

    def __init__(self, source: Any, max_lines: int) -> None:
        self._source = source
        self._max_lines = max_lines
        self._splitter = _PayloadSplitter()
        self._pending = bytearray()

    def _lines_from(self, payload: bytes) -> list[str]:
        self._pending += payload
        lines: list[str] = []
        while True:
            index = self._pending.find(b"\n")
            if index < 0:
                break
            lines.append(decode_log_line(bytes(self._pending[:index])))
            del self._pending[: index + 1]
        return lines

    def read(self) -> tuple[list[str], bool]:
        """Return ``(lines, truncated)``; stop reading once the cap is reached."""
        lines: list[str] = []
        truncated = False
        while not truncated:
            chunk = self._source.read(READ_CHUNK)
            if not chunk:
                break
            for payload in self._splitter.feed(chunk):
                for line in self._lines_from(payload):
                    if len(lines) >= self._max_lines:
                        truncated = True
                        break
                    lines.append(line)
                if truncated:
                    break
        if not truncated:
            for payload in (self._splitter.flush(),):
                if payload:
                    for line in self._lines_from(payload):
                        if len(lines) >= self._max_lines:
                            truncated = True
                            break
                        lines.append(line)
                if truncated:
                    break
        if not truncated and self._pending:
            if len(lines) >= self._max_lines:
                truncated = True
            else:
                lines.append(decode_log_line(bytes(self._pending)))
        self._pending.clear()
        return lines, truncated


class DockerClient:
    """Read-only Docker Engine client bound to one endpoint."""

    def __init__(self, base_url: str = DEFAULT_DOCKER_URL, *, timeout: float = 10.0) -> None:
        self.endpoint = parse_docker_url(base_url)
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.timeout = float(timeout)

    # -- transport ---------------------------------------------------------

    def _connect(self) -> http.client.HTTPConnection:
        if self.endpoint.scheme == "unix":
            return _UnixHTTPConnection(
                self.endpoint.socket_path or DEFAULT_SOCKET_PATH, self.timeout
            )
        return http.client.HTTPConnection(
            self.endpoint.host or "localhost", self.endpoint.port or 80, timeout=self.timeout
        )

    def _request(self, path: str) -> tuple[http.client.HTTPConnection, Any]:
        conn = self._connect()
        try:
            conn.request(
                "GET",
                f"{self.endpoint.path_prefix}{path}",
                headers={"Accept": "application/json", "Host": "docker"},
            )
            response = conn.getresponse()
        except (OSError, http.client.HTTPException) as error:
            conn.close()
            raise DockerUnavailable(
                f"cannot reach docker at {self.endpoint.display}: {error}"
            ) from error
        return conn, response

    def _read_json(self, path: str) -> Any:
        conn, response = self._request(path)
        try:
            if response.status != 200:
                raise DockerError(f"GET {path} returned HTTP {response.status}")
            body = response.read(MAX_JSON_BODY)
        except (OSError, http.client.HTTPException) as error:
            raise DockerError(f"GET {path} failed: {error}") from error
        finally:
            conn.close()
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DockerError(f"GET {path} returned invalid JSON: {error}") from error

    # -- endpoints ---------------------------------------------------------

    def list_containers(self) -> list[dict[str, Any]]:
        """``GET /containers/json?all=0``: the running containers."""
        document = self._read_json("/containers/json?all=0")
        if not isinstance(document, list):
            raise DockerError("GET /containers/json did not return a list")
        return [entry for entry in document if isinstance(entry, dict)]

    def inspect_container(self, container_id: str) -> dict[str, Any]:
        """``GET /containers/{id}/json``: restart count, state and image."""
        document = self._read_json(f"/containers/{quote(container_id, safe='')}/json")
        if not isinstance(document, dict):
            raise DockerError(
                f"GET /containers/{container_id}/json did not return an object"
            )
        return document

    def open_logs(
        self, container_id: str, *, since: int, tail: int
    ) -> tuple[http.client.HTTPConnection, Any]:
        """Open the log stream; the caller must close the connection."""
        query = (
            "stdout=1&stderr=1&timestamps=1"
            f"&since={int(since)}&tail={int(tail)}"
        )
        path = f"/containers/{quote(container_id, safe='')}/logs?{query}"
        conn, response = self._request(path)
        if response.status != 200:
            conn.close()
            raise DockerError(f"GET {path} returned HTTP {response.status}")
        return conn, response

    def read_log_lines(
        self, container_id: str, *, since: int, tail: int, max_lines: int
    ) -> tuple[list[str], bool]:
        """Return ``(lines, truncated)`` for one container, capped at ``max_lines``."""
        conn, response = self.open_logs(container_id, since=since, tail=tail)
        reader = LogStreamReader(response, max_lines)
        try:
            return reader.read()
        except (OSError, http.client.HTTPException) as error:
            raise DockerError(
                f"reading logs of {container_id} failed: {error}"
            ) from error
        finally:
            conn.close()
