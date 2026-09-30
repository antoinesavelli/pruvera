"""Build the docs index the retrieval tool searches: chunks of a fixture's markdown and yaml files.

Chunks are fixed-size (1,500 characters) and prefixed with their file path, embedded with
`nomic-embed-text` on the local Ollama. The index lives under `fixtures/paramo/rag/<version>/`
(gitignored, kept out of the backup: it holds fixture text). Needs numpy: run it with the fixture
venv's python. Depends on: numpy, a running Ollama.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import numpy as np

CHUNK = 1500
OLLAMA = "http://127.0.0.1:11434/api/embed"
SKIP = ("tests/golden/", ".git/", ".venv/", "docs/eval/")  # docs/eval holds the eval answer key


def chunks(tree: Path) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for path in sorted(tree.rglob("*")):
        rel = path.relative_to(tree).as_posix()
        if (
            not path.is_file()
            or path.suffix not in (".md", ".yaml", ".yml")
            or rel.startswith(SKIP)
        ):
            continue
        text = path.read_text(errors="replace")
        for i in range(0, max(len(text), 1), CHUNK):
            out.append({"file": rel, "text": f"{rel}\n{text[i : i + CHUNK]}"})
    return out


def embed_documents(texts: list[str]) -> np.ndarray:
    parts: list[list[float]] = []
    for i in range(0, len(texts), 64):
        body = json.dumps(
            {
                "model": "nomic-embed-text",
                "input": ["search_document: " + t for t in texts[i : i + 64]],
                "truncate": True,
            }
        ).encode()
        req = urllib.request.Request(OLLAMA, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            parts += json.load(resp)["embeddings"]
    matrix = np.array(parts, dtype=np.float32)
    normalised: np.ndarray = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    return normalised


def build(tree: Path, out: Path) -> dict[str, int]:
    """Write `out/chunks.json` and `out/vectors.npy`; returns counts."""
    out.mkdir(parents=True, exist_ok=False)
    items = chunks(tree)
    vectors = embed_documents([c["text"] for c in items])
    (out / "chunks.json").write_text(json.dumps(items))
    np.save(out / "vectors.npy", vectors)
    return {"chunks": len(items), "files": len({c["file"] for c in items})}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2] / "fixtures" / "paramo"
    print(json.dumps(build(root / "versions" / "v2" / "tree", root / "rag" / "v2")))
    sys.exit(0)
