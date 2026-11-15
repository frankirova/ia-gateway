"""Fake Docker Engine API and fake gateway for host-report tests.

The servers bind to loopback only. The Docker double speaks the three
endpoints the collector needs; the gateway double speaks one chat-completion
endpoint. Neither touches a real daemon or the network.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import urlsplit

CONTAINERS_JSON_ROUTE = re.compile(r"^(?:/v[\d.]+)?/containers/json$")
CONTAINER_INSPECT_ROUTE = re.compile(r"^(?:/v[\d.]+)?/containers/([^/]+)/json$")
CONTAINER_LOGS_ROUTE = re.compile(r"^(?:/v[\d.]+)?/containers/([^/]+)/logs$")

STDOUT = 1
STDERR = 2

DEFAULT_TS = "2024-05-01T10:00:00.000000000Z"


def log_line(text: str, ts: str = DEFAULT_TS) -> str:
    """Render one Docker log line, timestamp prefix included."""
    return f"{ts} {text}"


def mux_frames(frames: list[tuple[int, bytes]]) -> bytes:
    """Encode ``(stream, payload)`` pairs as a Docker multiplexed stream."""
    out = bytearray()
    for stream, payload in frames:
        out += bytes([stream, 0, 0, 0])
        out += len(payload).to_bytes(4, "big")
        out += payload
    return bytes(out)


def multiplexed_body(
    lines: list[str], *, lines_per_frame: int = 2, stream: int = STDOUT
) -> bytes:
    """Encode log lines as a multiplexed body, ``lines_per_frame`` per frame."""
    frames: list[tuple[int, bytes]] = []
    for index in range(0, len(lines), lines_per_frame):
        block = "".join(f"{line}\n" for line in lines[index : index + lines_per_frame])
        frames.append((stream, block.encode("utf-8")))
    return mux_frames(frames)


def plain_body(lines: list[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


@dataclass
class FakeContainer:
    """One container served by the fake Docker API."""

    id: str
    name: str
    image: str = "example/image:1"
    state: str = "running"
    restart_count: int = 0
    exit_code: int = 0
    log_lines: list[str] = field(default_factory=list)
    logs_body: bytes | None = None
    logs_mode: str = "multiplexed"
    logs_status: int = 200
    inspect_status: int = 200
    logs_delay: float = 0.0
    listed: bool = True

    def rendered_body(self) -> bytes:
        if self.logs_body is not None:
            return self.logs_body
        if self.logs_mode == "plain":
            return plain_body(self.log_lines)
        return multiplexed_body(self.log_lines)

    def listing_entry(self) -> dict[str, Any]:
        return {
            "Id": self.id,
            "Names": [f"/{self.name}"],
            "Image": self.image,
            "State": self.state,
            "Status": f"{self.state} (fake)",
        }

    def inspect_document(self) -> dict[str, Any]:
        return {
            "Id": self.id,
            "Name": f"/{self.name}",
            "RestartCount": self.restart_count,
            "State": {
                "Status": self.state,
                "Running": self.state == "running",
                "ExitCode": self.exit_code,
            },
            "Config": {"Image": self.image},
        }


class FakeDockerApi:
    """Loopback HTTP server that behaves like a small Docker daemon."""

    def __init__(self, containers: list[FakeContainer] | None = None) -> None:
        self.containers = list(containers or [])
        self.paths: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.api = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def record(self, raw_path: str) -> None:
        self.paths.append(raw_path)

    def find(self, container_id: str) -> FakeContainer | None:
        for container in self.containers:
            if container.id == container_id or container.name == container_id:
                return container
        return None

    def listing_document(self) -> list[dict[str, Any]]:
        return [c.listing_entry() for c in self.containers if c.listed]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - silence
        return None

    @property
    def api(self) -> FakeDockerApi:
        return self.server.api  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.api.record(self.path)
        path = urlsplit(self.path).path
        try:
            if CONTAINERS_JSON_ROUTE.match(path):
                self._send_json(200, self.api.listing_document())
                return
            match = CONTAINER_INSPECT_ROUTE.match(path)
            if match:
                self._serve_inspect(match.group(1))
                return
            match = CONTAINER_LOGS_ROUTE.match(path)
            if match:
                self._serve_logs(match.group(1))
                return
            self._send_json(404, {"message": "page not found"})
        except (BrokenPipeError, ConnectionResetError):
            return None

    def _serve_inspect(self, container_id: str) -> None:
        container = self.api.find(container_id)
        if container is None or container.inspect_status != 200:
            self._send_json(404, {"message": "No such container"})
            return
        self._send_json(200, container.inspect_document())

    def _serve_logs(self, container_id: str) -> None:
        container = self.api.find(container_id)
        if container is None:
            self._send_json(404, {"message": "No such container"})
            return
        if container.logs_delay:
            time.sleep(container.logs_delay)
        if container.logs_status != 200:
            self._send_json(container.logs_status, {"message": "logs unavailable"})
            return
        body = container.rendered_body()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.docker.raw-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def fake_docker(*containers: FakeContainer) -> Iterator[FakeDockerApi]:
    api = FakeDockerApi(list(containers))
    try:
        yield api
    finally:
        api.stop()


GATEWAY_COMPLETIONS_PATH = "/v1/chat/completions"


@dataclass
class FakeGatewayReply:
    """What the fake gateway answers with.

    ``body`` overrides the synthesised completion body, which lets a test send
    something the client must not echo verbatim.
    """

    status: int = 200
    content: str = '{"summary": "Quiet host.", "findings": []}'
    body: bytes | None = None
    delay: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)

    def rendered_body(self) -> bytes:
        if self.body is not None:
            return self.body
        document = {
            "id": "fake-completion",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self.content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return json.dumps(document).encode("utf-8")


class FakeGatewayApi:
    """Loopback HTTP server that behaves like the inference gateway."""

    def __init__(self, reply: FakeGatewayReply | None = None) -> None:
        self.reply = FakeGatewayReply() if reply is None else reply
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self._server.daemon_threads = True
        self._server.api = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.02),
            daemon=True,
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def last_request(self) -> dict[str, Any]:
        return self.requests[-1]

    def record(self, request: dict[str, Any]) -> None:
        self.requests.append(request)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - silence
        return None

    @property
    def api(self) -> FakeGatewayApi:
        return self.server.api  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body: Any = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        self.api.record(
            {
                "method": "POST",
                "path": urlsplit(self.path).path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
                "raw_body": raw,
            }
        )
        reply = self.api.reply
        if reply.delay:
            time.sleep(reply.delay)
        payload = reply.rendered_body()
        try:
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.api.record(
            {
                "method": "GET",
                "path": urlsplit(self.path).path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": None,
                "raw_body": b"",
            }
        )
        payload = json.dumps({"error": {"code": "not_found"}}).encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextlib.contextmanager
def fake_gateway(reply: FakeGatewayReply | None = None) -> Iterator[FakeGatewayApi]:
    api = FakeGatewayApi(reply)
    try:
        yield api
    finally:
        api.stop()


def closed_port_url() -> str:
    """Return an ``http://127.0.0.1:<port>`` URL that refuses connections."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()
    return f"http://127.0.0.1:{port}"


@dataclass
class FakeHubReply:
    """What the fake hub answers with.

    ``body`` overrides the default ``{"status": "sent"}`` document, which lets a
    test drive the duplicate and failure paths without a second handler.
    """

    status: int = 200
    body: dict[str, Any] | None = None
    delay: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)

    def rendered_body(self) -> bytes:
        document = {"status": "sent"} if self.body is None else self.body
        return json.dumps(document).encode("utf-8")


class FakeHubApi:
    """Loopback HTTP server that behaves like the personal hub's ``/send``."""

    def __init__(self, reply: FakeHubReply | None = None) -> None:
        self.reply = FakeHubReply() if reply is None else reply
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _HubHandler)
        self._server.daemon_threads = True
        self._server.api = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.02),
            daemon=True,
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def last_request(self) -> dict[str, Any]:
        return self.requests[-1]

    def record(self, request: dict[str, Any]) -> None:
        self.requests.append(request)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _HubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - silence
        return None

    @property
    def api(self) -> FakeHubApi:
        return self.server.api  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body: Any = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        self.api.record(
            {
                "method": "POST",
                "path": urlsplit(self.path).path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
                "raw_body": raw,
            }
        )
        reply = self.api.reply
        if reply.delay:
            time.sleep(reply.delay)
        payload = reply.rendered_body()
        try:
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return None


@contextlib.contextmanager
def garbage_hub(payload: bytes = b"this is not http at all\r\n\r\n") -> Iterator[str]:
    """A TCP listener that answers with a body that is not HTTP at all.

    Returns its base URL. It proves the client never lets a transport-level
    exception escape instead of a plain delivery failure.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()[:2]

    def serve() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                try:
                    connection.recv(65_536)
                    connection.sendall(payload)
                except OSError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        listener.close()
        thread.join(timeout=5)


@contextlib.contextmanager
def fake_hub(reply: FakeHubReply | None = None) -> Iterator[FakeHubApi]:
    api = FakeHubApi(reply)
    try:
        yield api
    finally:
        api.stop()
