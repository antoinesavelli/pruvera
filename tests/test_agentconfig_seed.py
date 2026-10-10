"""Tests for the opt-in sampling seed: its place in the assembled config, and opencode sending it.

A trial is unseeded by default (a real session is); `seed=N` is for variance studies only. The wire
test runs the real opencode in the sandbox against a fake Ollama and reads the request bodies.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bench import agentconfig, runner, sandbox
from tests.helpers import bwrap_works as _bwrap_works
from tests.helpers import git

OPENCODE = Path.home() / ".opencode" / "bin" / "opencode"
RIPGREP = Path.home() / ".cache" / "opencode" / "bin" / "rg"

REAL = {
    "provider": {
        "ollama": {
            "options": {"baseURL": "http://localhost:11434/v1"},
            "models": {"a": {"name": "a"}, "b": {"name": "b", "options": {"temperature": 0.2}}},
        },
        "openrouter": {"options": {"apiKey": "{env:K}"}},
    },
    "agent": {"git": {"model": "ollama/a"}, "plan": {"model": "openrouter/x"}},
}


def _real(tmp_path: Path) -> Path:
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps(REAL))
    return path


def _inline_models(asm: agentconfig.Assembly) -> dict[str, Any]:
    models: dict[str, Any] = asm.inline["provider"]["ollama"]["models"]
    return models


def test_a_trial_is_unseeded_by_default(tmp_path: Path) -> None:
    asm = agentconfig.assemble("git", "m", _real(tmp_path))
    assert asm.seed is None
    assert asm.inline == {"agent": {"git": {"model": "ollama/m"}}}
    assert "seed" not in asm.env()["OPENCODE_CONFIG_CONTENT"]


def test_a_seed_goes_in_the_options_of_every_ollama_model_the_trial_can_reach(
    tmp_path: Path,
) -> None:
    asm = agentconfig.assemble("git", "m", _real(tmp_path), seed=4242)
    assert asm.seed == 4242
    # `m` is the model under test and is not listed in the real config; `a` and `b` are listed.
    assert set(_inline_models(asm)) == {"a", "b", "m"}
    for options in (spec["options"] for spec in _inline_models(asm).values()):
        assert options == {"seed": 4242}
    assert asm.inline["agent"] == {"git": {"model": "ollama/m"}}
    assert json.loads(asm.env()["OPENCODE_CONFIG_CONTENT"]) == asm.inline


def test_a_seed_does_not_change_the_global_config_or_its_parity(tmp_path: Path) -> None:
    plain = agentconfig.assemble("git", "m", _real(tmp_path))
    seeded = agentconfig.assemble("git", "m", _real(tmp_path), seed=7)
    assert seeded.config == plain.config and seeded.deviations == plain.deviations
    assert seeded.real_sha256 == plain.real_sha256
    agentconfig.check_parity(REAL, seeded.config, seeded.deviations)


def test_the_inline_seed_lays_over_the_global_models_and_keeps_their_other_options(
    tmp_path: Path,
) -> None:
    asm = agentconfig.assemble("git", "m", _real(tmp_path), seed=7)
    merged = runner.merge(asm.config, asm.inline)
    models = merged["provider"]["ollama"]["models"]
    assert models["a"] == {"name": "a", "options": {"seed": 7}}
    assert models["b"] == {"name": "b", "options": {"temperature": 0.2, "seed": 7}}
    assert models["m"] == {"name": "m", "options": {"seed": 7}}


def test_seed_zero_is_a_seed_not_no_seed(tmp_path: Path) -> None:
    asm = agentconfig.assemble("git", "m", _real(tmp_path), seed=0)
    assert asm.seed == 0 and _inline_models(asm)["m"]["options"] == {"seed": 0}


@pytest.mark.parametrize("bad", [-1, 2**31, True, 1.5, "7"])
def test_a_seed_that_would_not_be_one_is_refused(bad: Any, tmp_path: Path) -> None:
    # -1 is Ollama's "random": recording it as a seed would be false.
    with pytest.raises(ValueError, match="seed"):
        agentconfig.assemble("git", "m", _real(tmp_path), seed=bad)
    with pytest.raises(ValueError, match="seed"):
        agentconfig.check_seed(bad)


def test_check_seed_accepts_none_and_the_bounds() -> None:
    for fine in (None, 0, 1, 2**31 - 1):
        agentconfig.check_seed(fine)


class _FakeOllama(http.server.BaseHTTPRequestHandler):
    """Records each chat request body and answers with one streamed completion."""

    protocol_version = "HTTP/1.0"
    bodies: list[dict[str, Any]]

    def log_message(self, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.bodies.append(json.loads(body))
        base = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "m"}
        delta = {"index": 0, "delta": {"role": "assistant", "content": "ok"}}
        done = {"index": 0, "delta": {}, "finish_reason": "stop"}
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        events = [{**base, "choices": [delta]}, {**base, "choices": [done], "usage": usage}]
        payload = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(payload.encode())


@pytest.fixture
def fake_ollama() -> Iterator[tuple[int, list[dict[str, Any]]]]:
    bodies: list[dict[str, Any]] = []
    handler = type("Handler", (_FakeOllama,), {"bodies": bodies})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield port, bodies
    server.shutdown()
    server.server_close()


def _ask_opencode(tmp_path: Path, port: int, seed: int | None) -> None:
    """Run the real opencode once in the sandbox, its model served by the fake on `port`."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "AGENTS.md").write_text("rules\n")
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "base")
    real = {
        "provider": {
            "ollama": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Ollama",
                "options": {"baseURL": f"http://localhost:{port}/v1"},
                "models": {"m": {"name": "m"}, "other": {"name": "other"}},
            }
        },
        "agent": {"a": {"model": "ollama/m", "prompt": "Reply with the word ok."}},
    }
    path = tmp_path / "real.json"
    path.write_text(json.dumps(real))
    asm = agentconfig.assemble("a", "m", path, seed=seed)
    dirs = {n: tmp_path / "t" / n for n in ("upper", "work", "xdg/config", "xdg/data", "xdg/state")}
    for d in dirs.values():
        d.mkdir(parents=True)
    agentconfig.write(asm, dirs["xdg/config"])
    spec = sandbox.Spec(
        base=tree,
        upper=dirs["upper"],
        work=dirs["work"],
        xdg=tmp_path / "t" / "xdg",
        ro_binds=(
            (OPENCODE.resolve(), "/opt/bin/opencode"),
            (RIPGREP.resolve(), f"{sandbox.HOME}/.cache/opencode/bin/rg"),
        ),
        env={"PATH": "/opt/bin:/usr/bin:/bin", **asm.env()},
        net="ollama",
        ollama_port=port,
        ollama_models=frozenset({"m", "other"}),
    )
    script = f"cd {sandbox.WORKDIR} && opencode run --agent a --format json 'say ok'"
    try:
        done = sandbox.run(spec, ["sh", "-c", script], timeout=180)
    finally:
        sandbox.remove_trial_dirs(tmp_path / "t")
    assert done.returncode == 0, done.stderr[-300:]


needs_opencode = pytest.mark.skipif(
    not _bwrap_works() or not OPENCODE.exists() or not RIPGREP.exists(),
    reason="needs bwrap, opencode and ripgrep",
)


@needs_opencode
def test_opencode_sends_the_seed_with_every_chat_request_when_one_is_set(
    tmp_path: Path, fake_ollama: tuple[int, list[dict[str, Any]]]
) -> None:
    port, bodies = fake_ollama
    _ask_opencode(tmp_path, port, 4242)
    chats = [b for b in bodies if "messages" in b]
    assert chats, "opencode made no chat request"
    assert [b.get("seed") for b in chats] == [4242] * len(chats)


@needs_opencode
def test_opencode_sends_no_seed_when_none_is_set(
    tmp_path: Path, fake_ollama: tuple[int, list[dict[str, Any]]]
) -> None:
    port, bodies = fake_ollama
    _ask_opencode(tmp_path, port, None)
    chats = [b for b in bodies if "messages" in b]
    assert chats, "opencode made no chat request"
    assert not any("seed" in b for b in chats)
