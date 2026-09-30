"""Spike: does embedding retrieval find a knowledge question's source files better than keywords?

Corpus: every tracked .md/.yaml file of the real repo at the pinned commit, read through git so the
working tree is never touched, chunked. Questions: docs/eval/knowledge_questions.yaml (each names
its source files). Retrievers: BM25 (no model) and nomic-embed-text on the local Ollama, brute-force
cosine in numpy (no vector database). Metric: file-level recall@k and MRR of the first declared
source. Writes only file names and scores. Run with Paramo's venv python (numpy, pyyaml).
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

import numpy as np
import yaml

REPO = Path("/mnt/ParamoStorage/Paramo")
COMMIT = "c65a28899774110e5cf19d66e5eddb6c674a826b"
CHUNK = 1500
OLLAMA = "http://127.0.0.1:11434/api/embed"
TOKEN = re.compile(r"[a-z0-9_]+")


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True).stdout


def corpus() -> list[tuple[str, str]]:
    files = [
        f
        for f in git("ls-tree", "-r", "--name-only", COMMIT).decode().splitlines()
        if f.endswith((".md", ".yaml", ".yml")) and not f.startswith("tests/golden/")
    ]
    chunks = []
    for f in files:
        text = git("show", "--textconv", f"{COMMIT}:{f}").decode("utf-8", "replace")
        if text.startswith("\x00GITCRYPT"):
            continue
        for i in range(0, max(len(text), 1), CHUNK):
            chunks.append((f, f"{f}\n{text[i : i + CHUNK]}"))
    return chunks


def bm25(chunks: list[tuple[str, str]], query: str, k1: float = 1.5, b: float = 0.75) -> np.ndarray:
    docs = [TOKEN.findall(t.lower()) for _, t in chunks]
    n, avg = len(docs), sum(map(len, docs)) / len(docs)
    df = Counter(w for d in docs for w in set(d))
    q = TOKEN.findall(query.lower())
    scores = np.zeros(n)
    for i, d in enumerate(docs):
        tf = Counter(d)
        for w in q:
            if w in tf:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                scores[i] += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(d) / avg))
    return scores


def embed(texts: list[str], prefix: str) -> np.ndarray:
    out = []
    for i in range(0, len(texts), 64):
        body = json.dumps(
            {
                "model": "nomic-embed-text",
                "input": [prefix + t for t in texts[i : i + 64]],
                "truncate": True,
            }
        ).encode()
        req = urllib.request.Request(OLLAMA, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out += json.load(r)["embeddings"]
    m = np.array(out, dtype=np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def rank_files(chunks: list[tuple[str, str]], scores: np.ndarray) -> list[str]:
    seen: list[str] = []
    for i in np.argsort(-scores):
        f = chunks[i][0]
        if f not in seen:
            seen.append(f)
        if len(seen) == 20:
            break
    return seen


def main() -> int:
    qs = yaml.safe_load((REPO / "docs/eval/knowledge_questions.yaml").read_text())
    chunks = corpus()
    t0 = time.time()
    vecs = embed([t for _, t in chunks], "search_document: ")
    t_index = time.time() - t0
    qvecs = embed([q["question"] for q in qs], "search_query: ")
    results = {"bm25": [], "embed": [], "hybrid": []}
    for q, qv in zip(qs, qvecs, strict=True):
        s_b = bm25(chunks, q["question"])
        s_e = vecs @ qv
        z = lambda a: (a - a.mean()) / (a.std() + 1e-9)  # noqa: E731
        for name, s in (("bm25", s_b), ("embed", s_e), ("hybrid", z(s_b) + z(s_e))):
            ranked = rank_files(chunks, s)
            hit = [r + 1 for r, f in enumerate(ranked) if f in q.get("sources", [])]
            results[name].append(hit[0] if hit else None)
    summary = {
        "questions": len(qs),
        "chunks": len(chunks),
        "files": len({f for f, _ in chunks}),
        "index_seconds": round(t_index, 1),
    }
    for name, firsts in results.items():
        n = len(firsts)
        summary[name] = {
            f"recall@{k}": round(sum(1 for r in firsts if r and r <= k) / n, 3)
            for k in (1, 3, 5, 10)
        } | {"mrr": round(sum(1 / r for r in firsts if r) / n, 3)}
    print(json.dumps(summary, indent=1))
    Path(__file__).with_suffix(".result.json").write_text(json.dumps(summary, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
