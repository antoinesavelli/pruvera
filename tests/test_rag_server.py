"""Tests for the docs-search MCP server: protocol shapes, ranking, stub embedder (needs numpy)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")

from bench.rag import index as rag_index  # noqa: E402
from bench.rag import server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / "fixtures/paramo/venv/v2/bin/python"
DOCS = {
    "docs/strategy.md": "the operative default strategy is insider cluster, a long strategy",
    "docs/kelly.md": "kelly sizing fraction and the volume cap are applied last",
    "docs/golden.md": "golden regression baseline and rebless rules",
}


def reply(idx: server.Index, message: dict[str, Any]) -> dict[str, Any]:
    """The server's reply to a request; a request must get one."""
    out = server.handle(idx, message)
    assert out is not None
    return out


@pytest.fixture
def index_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RAG_EMBED", "stub")
    items = [{"file": f, "text": f"{f}\n{t}"} for f, t in DOCS.items()]
    vectors = np.array([server.stub_embed(i["text"]) for i in items])
    out = tmp_path / "idx"
    out.mkdir()
    (out / "chunks.json").write_text(json.dumps(items))
    np.save(out / "vectors.npy", vectors)
    return out


def test_search_ranks_the_relevant_file_first_and_returns_distinct_files(index_dir: Path) -> None:
    idx = server.Index(index_dir)
    hits = idx.search("which strategy is the operative default", top_k=3)
    assert hits[0][1] == "docs/strategy.md" and len({h[1] for h in hits}) == len(hits)
    assert server.Index(index_dir).search("kelly volume cap", top_k=1)[0][1] == "docs/kelly.md"
    assert len(idx.search("anything", top_k=99)) == 3, "top_k is capped by the corpus"


def test_protocol_initialize_list_call_and_errors(index_dir: Path) -> None:
    idx = server.Index(index_dir)
    init = reply(
        idx,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        },
    )
    assert (
        init["result"]["protocolVersion"] == "2025-03-26"
        and "tools" in init["result"]["capabilities"]
    )
    assert server.handle(idx, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    listed = reply(idx, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == ["search_docs"]
    call = reply(
        idx,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search_docs", "arguments": {"query": "golden rebless", "top_k": 2}},
        },
    )
    text = call["result"]["content"][0]["text"]
    assert "docs/golden.md" in text and call["result"]["isError"] is False
    bad = reply(
        idx,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "search_docs", "arguments": {"query": "  "}},
        },
    )
    assert bad["error"]["code"] == -32602
    assert reply(idx, {"jsonrpc": "2.0", "id": 5, "method": "nope"})["error"]["code"] == -32601


def test_the_server_speaks_json_lines_over_stdio(index_dir: Path) -> None:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    env = {**os.environ, "RAG_INDEX_DIR": str(index_dir), "RAG_EMBED": "stub"}
    done = subprocess.run(
        [str(VENV_PYTHON) if VENV_PYTHON.exists() else "python3", "-m", "bench.rag.server"],
        input="\n".join(json.dumps(r) for r in requests) + "\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=60,
    )
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2], done.stderr[-300:]


def test_chunks_skip_golden_and_non_docs_and_prefix_each_chunk_with_its_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x" * 3200)
    (tmp_path / "docs" / "b.py").write_text("code")
    (tmp_path / "tests" / "golden").mkdir(parents=True)
    (tmp_path / "tests" / "golden" / "r.md").write_text("skip me")
    items = rag_index.chunks(tmp_path)
    assert [i["file"] for i in items] == ["docs/a.md"] * 3
    assert all(i["text"].startswith("docs/a.md\n") for i in items)


@pytest.mark.parametrize(
    "message",
    [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_docs", "arguments": {"query": "kelly", "top_k": "five"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_docs", "arguments": {"query": "kelly", "top_k": None}},
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_docs", "arguments": ["hi"]},
        },
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": None},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_docs", "arguments": {"query": 7}},
        },
    ],
)
def test_a_malformed_call_is_a_bad_call_reply_not_a_crash(
    index_dir: Path, message: dict[str, Any]
) -> None:
    idx = server.Index(index_dir)
    assert server.handle(idx, message)["error"]["code"] == -32602  # type: ignore[index]


def test_odd_but_harmless_requests_get_replies_and_a_bad_line_does_not_end_the_server(
    index_dir: Path,
) -> None:
    idx = server.Index(index_dir)
    assert server.handle(idx, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": None})[
        "result"
    ]["protocolVersion"]  # type: ignore[index]
    assert server.handle(idx, [1, 2, 3])["error"]["code"] == -32600  # type: ignore[index]
    assert server._reply(idx, "not json")["error"]["code"] == -32700  # type: ignore[index]
    ok = server._reply(idx, '{"jsonrpc": "2.0", "id": 3, "method": "ping"}')
    assert ok == {"jsonrpc": "2.0", "id": 3, "result": {}}
