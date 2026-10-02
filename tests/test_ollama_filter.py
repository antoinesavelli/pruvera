"""Tests for the Ollama filter: only inference passes, every host-changing call is refused."""

from __future__ import annotations

import http.client
import http.server
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from bench import ollama_filter


class _Fake(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []
    hosts: list[str] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _Fake.seen.append((self.command, self.path))
        _Fake.hosts += self.headers.get_all("Host") or []
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"echo:" + body)

    do_GET = do_POST = do_DELETE = _reply


@pytest.fixture
def upstream() -> Iterator[int]:
    _Fake.seen = []
    _Fake.hosts = []
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
    with ollama_filter.serve(sock, upstream, frozenset({"m"})):
        assert sock.stat().st_mode & 0o777 == 0o600
        status, data = call(sock, "POST", "/v1/chat/completions", b'{"model": "m"}')
        assert status == 200 and data == b'echo:{"model":"m"}', "upstream reads the canonical body"
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
    with ollama_filter.serve(sock, upstream, frozenset({"m"})):
        for _ in range(20):
            assert call(sock, "POST", "/api/pull", b"x" * 300_000)[0] == 403
        assert call(sock, "POST", "/api/chat", b'{"model": "m"}')[0] == 200


def test_model_allowlist_refuses_other_models_but_not_requests_without_one() -> None:
    models = frozenset({"gpt-oss:20b"})
    assert ollama_filter.model_allowed(b'{"model": "gpt-oss:20b"}', models)
    assert not ollama_filter.model_allowed(b'{"model": "llama3:70b"}', models)
    assert not ollama_filter.model_allowed(b"{not json", models)
    assert not ollama_filter.model_allowed(b'{"model": ["gpt-oss:20b"]}', models)
    assert ollama_filter.model_allowed(b'{"messages": []}', models)
    assert ollama_filter.model_allowed(None, models)
    assert not ollama_filter.model_allowed(b'{"model": "anything"}', frozenset()), "fails closed"


def test_the_model_gate_cannot_be_dodged_by_key_case_alias_or_duplicates() -> None:
    """Regression: Ollama's decoder is case-insensitive and /api/show reads `name` too."""
    models = frozenset({"gpt-oss:20b"})
    for evil in (
        b'{"MODEL": "x"}',
        b'{"Model": "x"}',
        b'{"name": "x"}',
        b'{"model": "gpt-oss:20b", "Model": "x"}',
        b'{"model": "gpt-oss:20b", "model": "x"}',
        b'["model"]',
        b'"model"',
    ):
        assert not ollama_filter.model_allowed(evil, models), evil
    assert ollama_filter.model_allowed(b'{"name": "gpt-oss:20b"}', models)
    assert not ollama_filter.model_allowed(b'{"NAME": "gpt-oss:20b"}', models), "no upper-case keys"


def test_conflicting_or_duplicate_content_length_headers_are_refused(
    tmp_path: Path, upstream: int
) -> None:
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, upstream, frozenset({"m"})):
        raw = (
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n"
            b"Content-Length: 50\r\n\r\n{}"
        )
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock))
        client.sendall(raw)
        reply = client.recv(4096)
        client.close()
    assert reply.startswith(b"HTTP/1.0 400") and not _Fake.seen


def test_served_with_an_allowlist_blocks_a_model_that_is_not_listed(
    tmp_path: Path, upstream: int
) -> None:
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, upstream, frozenset({"ok:1b"})):
        assert call(sock, "POST", "/v1/chat/completions", b'{"model": "ok:1b"}')[0] == 200
        assert call(sock, "POST", "/v1/chat/completions", b'{"model": "huge:405b"}')[0] == 403
    assert all("huge" not in path for _, path in _Fake.seen)


def test_deeply_nested_json_is_refused_not_a_crash() -> None:
    """Regression: 200k opening brackets raised RecursionError instead of being refused."""
    deep = b"[" * 200_000
    assert not ollama_filter.model_allowed(deep, frozenset({"m"}))


def test_a_client_that_stalls_mid_request_is_dropped_after_the_read_timeout(
    tmp_path: Path, upstream: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ollama_filter, "READ_TIMEOUT", 0.4)
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, upstream, frozenset({"m"})):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock))
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\\r\\nContent-Length: 1000\\r\\n\\r\\n{")
        client.settimeout(3)
        assert client.recv(4096) == b"", "the filter hangs up on a stalled sender"
        client.close()
        assert call(sock, "GET", "/api/tags")[0] == 200, "and keeps serving others"


def test_a_unicode_digit_content_length_is_a_clean_400_not_a_crash(
    tmp_path: Path, upstream: int
) -> None:
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, upstream, frozenset({"m"})):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock))
        client.sendall("POST /v1/chat/completions HTTP/1.1\r\nContent-Length: ²\r\n\r\n".encode())
        reply = client.recv(4096)
        client.close()
    assert reply.startswith(b"HTTP/1.0 400") and not _Fake.seen


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        (b'{"model": "m", "options": {"num_ctx": 65536}}', True),
        (b'{"model": "m", "options": {"num_ctx": 100000000}}', False),
        (b'{"model": "m", "options": {"NUM_CTX": 100000000}}', False),
        (b'{"model": "m", "num_ctx": "big"}', False),
        (b'{"model": "m", "keep_alive": -1}', False),
        (b'{"model": "m", "keep_alive": "-1"}', False),
        (b'{"model": "m", "keep_alive": "24h"}', False),
        (b'{"model": "m", "keep_alive": "5m"}', True),
        (b'{"model": "m", "keep_alive": 0}', True),
        (b'{"model": "m", "keep_alive": true}', False),
    ],
)
def test_requests_may_not_resize_the_context_or_pin_the_model(body: bytes, ok: bool) -> None:
    assert ollama_filter.model_allowed(body, frozenset({"m"})) is ok


def test_a_connection_beyond_the_slots_is_told_503_not_queued_in_a_thread(
    tmp_path: Path, upstream: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ollama_filter, "MAX_CONNECTIONS", 1)
    monkeypatch.setattr(ollama_filter, "SLOT_WAIT", 0.2)
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, upstream, frozenset({"m"})):
        holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        holder.connect(str(sock))
        holder.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nContent-Le")  # holds the one slot
        status = call(sock, "GET", "/api/tags")[0]
        holder.close()
    assert status == 503


def test_trace_records_request_shapes_and_latencies_but_never_text(
    tmp_path: Path, upstream: int
) -> None:
    trace = tmp_path / "trace.jsonl"
    sock = tmp_path / "f.sock"
    secret = "def secret_strategy(): pass"
    body = (
        b'{"model": "m", "tools": [1], "messages": ['
        b'{"role": "system", "content": "' + secret.encode() + b'"},'
        b'{"role": "user", "content": "hi"}]}'
    )
    with ollama_filter.serve(sock, upstream, frozenset({"m"}), trace):
        assert call(sock, "POST", "/v1/chat/completions", body)[0] == 200
        assert call(sock, "POST", "/v1/chat/completions", body)[0] == 200
    lines = [json.loads(x) for x in trace.read_text().splitlines()]
    assert len(lines) == 2 and lines[0]["n_messages"] == 2 and lines[0]["status"] == 200
    assert lines[0]["system"] == lines[1]["system"] and lines[0]["prefix"] == lines[1]["prefix"]
    assert lines[0]["first_byte_s"] >= 0 and lines[0]["total_s"] >= lines[0]["first_byte_s"]
    assert "secret_strategy" not in trace.read_text()


@pytest.mark.parametrize(
    "body",
    [
        b'{"model":"m","options":{"num_ctx":999999999},"Options":{"x":1}}',
        b'{"model":"m","options":{"num_ctx":999999999,"NUM_CTX":4096}}',
        b'{"model":"m","Options":{"num_ctx":999999999}}',
        b'{"model":"m","options":{"num_gpu":0}}',
        b'{"model":"m","options":{"num_thread":1000}}',
        b'{"model":"m","options":{"num_ctx":true}}',
        b'{"model":"m","options":{"num_predict":99999999}}',
        b'{"model":"m","options":[1]}',
        b'{"model":"m","num_ctx":1,"NUM_CTX":2}',
    ],
)
def test_option_smuggling_by_case_alias_or_unknown_key_is_refused(body: bytes) -> None:
    """Regression: Ollama merges case variants of `options` by rules the filter cannot see."""
    assert not ollama_filter.model_allowed(body, frozenset({"m"}))


def test_plain_sampling_options_still_pass() -> None:
    body = b'{"model":"m","options":{"temperature":0.2,"num_ctx":65536,"num_predict":-1,"seed":3}}'
    assert ollama_filter.model_allowed(body, frozenset({"m"}))


def test_connections_beyond_the_thread_cap_get_no_thread(
    tmp_path: Path, upstream: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ollama_filter, "MAX_THREADS", 2)
    monkeypatch.setattr(ollama_filter, "READ_TIMEOUT", 3.0)
    sock = tmp_path / "f.sock"
    held = []
    with ollama_filter.serve(sock, upstream, frozenset({"m"})):
        for _ in range(2):  # two idle connections hold the two threads
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock))
            held.append(c)
        time.sleep(0.3)
        extra = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        extra.connect(str(sock))
        extra.settimeout(3)
        assert extra.recv(100).startswith(b"HTTP/1.0 503")
        extra.close()
        for c in held:
            c.close()


def test_the_trace_stops_growing_at_its_cap_and_logs_no_query_string(
    tmp_path: Path, upstream: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ollama_filter, "TRACE_MAX_BYTES", 300)
    trace = tmp_path / "t.jsonl"
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, upstream, frozenset({"m"}), trace):
        for _ in range(8):
            call(sock, "GET", "/api/tags?" + "q" * 500)
    text = trace.read_text()
    assert len(text) < 700 and "qqqq" not in text


@pytest.mark.parametrize(
    "body",
    [
        '{"model":"m","optionſ":{"num_gpu":0}}'.encode(),
        b'{"model":"m","Keep_alive":"99999h"}',
        '{"model":"m","options":{"temperature":1,"ſeed":3}}'.encode(),
        b'{"model":"m","stop":[' + b",".join([b'"x"'] * 100) + b"]}",
        b'{"model":"m","options":{"stop":["' + b"y" * 500 + b'"]}}',
    ],
)
def test_unicode_case_folded_keys_and_huge_stop_lists_are_refused(body: bytes) -> None:
    """Regression: Python's `lower()` does not fold U+017F or U+212A, Go's JSON decoder does."""
    assert not ollama_filter.model_allowed(body, frozenset({"m"}))


def test_the_body_forwarded_upstream_is_the_one_the_filter_parsed(
    tmp_path: Path, upstream: int
) -> None:
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, upstream, frozenset({"m"})):
        status, data = call(sock, "POST", "/v1/chat/completions", b'{ "model" :  "m" ,"x":[1, 2]}')
    assert status == 200 and data == b'echo:{"model":"m","x":[1,2]}'
    assert ollama_filter.canonical(None) is None and ollama_filter.canonical(b"") == b""


def test_a_lower_case_host_header_is_not_forwarded_and_a_non_ascii_path_is_a_clean_502(
    tmp_path: Path, upstream: int
) -> None:
    sock = tmp_path / "f.sock"
    with ollama_filter.serve(sock, upstream, frozenset({"m"})):
        for target, expected in (("/api/tags", b"200"), ("/api/tags?q=é", b"502")):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(sock))
            client.sendall(f"GET {target} HTTP/1.1\r\nhost: evil.example\r\n\r\n".encode())
            reply = client.recv(4096)
            client.close()
            assert reply.split(b" ", 2)[1] == expected, reply
    assert _Fake.hosts and all(host.startswith("127.0.0.1") for host in _Fake.hosts)
