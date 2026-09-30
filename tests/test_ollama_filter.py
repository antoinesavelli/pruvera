"""Tests for the Ollama filter: only inference passes, every host-changing call is refused."""

from __future__ import annotations

import http.client
import http.server
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from bench import ollama_filter


class _Fake(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _Fake.seen.append((self.command, self.path))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"echo:" + body)

    do_GET = do_POST = do_DELETE = _reply


@pytest.fixture
def upstream() -> Iterator[int]:
    _Fake.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def call(sock: Path, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
    conn = _UnixConn(str(sock))
    conn.request(method, path, body)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_allowlist_passes_inference_and_refuses_everything_that_changes_the_host() -> None:
    for method, path in (
        ("POST", "/v1/chat/completions"),
        ("GET", "/v1/models"),
        ("POST", "/api/embed"),
        ("GET", "/api/tags?x=1"),
    ):
        assert ollama_filter.allowed(method, path), (method, path)
    for method, path in (
        ("POST", "/api/pull"),
        ("DELETE", "/api/delete"),
        ("POST", "/api/copy"),
        ("POST", "/api/create"),
        ("POST", "/api/push"),
        ("POST", "/api/blobs/sha256:abc"),
        ("GET", "/v1/chat/completions"),
        ("POST", "/v1//chat/completions"),
        ("POST", "/v1/../api/pull"),
        ("POST", "/api/%70ull"),
        ("POST", "/"),
    ):
        assert not ollama_filter.allowed(method, path), (method, path)


def test_served_over_a_unix_socket_forwards_allowed_and_blocks_the_rest(
    tmp_path: Path, upstream: int
) -> None:
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, upstream):
        assert sock.stat().st_mode & 0o777 == 0o600
        status, data = call(sock, "POST", "/v1/chat/completions", b'{"model": "m"}')
        assert status == 200 and data == b'echo:{"model": "m"}'
        assert call(sock, "GET", "/api/tags")[0] == 200
        for method, path in (
            ("POST", "/api/pull"),
            ("DELETE", "/api/delete"),
            ("POST", "/api/copy"),
        ):
            assert call(sock, method, path, b"{}")[0] == 403
    assert ("POST", "/api/pull") not in _Fake.seen, "a refused call never reaches Ollama"
    assert ("DELETE", "/api/delete") not in _Fake.seen


def test_unreachable_upstream_is_a_502_not_a_hang(tmp_path: Path) -> None:
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, port):
        assert call(sock, "GET", "/api/tags")[0] == 502


def test_a_refused_call_with_a_large_body_gets_a_clean_403_not_a_reset(
    tmp_path: Path, upstream: int
) -> None:
    """Regression: refusing without reading the body made the client see a connection reset."""
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, upstream):
        for _ in range(20):
            assert call(sock, "POST", "/api/pull", b"x" * 300_000)[0] == 403
        assert call(sock, "POST", "/api/chat", b"x" * 10)[0] == 200
