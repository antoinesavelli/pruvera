"""Tests for bench.transcript on hand-made event streams in opencode's --format json shape."""

from __future__ import annotations

import json

from bench.transcript import Transcript


def _event(kind: str, part: dict[str, object], ts: int = 1) -> str:
    return json.dumps({"type": kind, "timestamp": ts, "part": part})


def test_counts_tools_text_tokens_and_errors() -> None:
    lines = [
        _event("step_start", {}, 10),
        _event(
            "tool_use",
            {"tool": "glob", "state": {"status": "error", "input": {"pattern": "x"}}},
            11,
        ),
        _event("tool_use", {"tool": "read", "state": {"status": "completed", "input": {}}}, 12),
        _event("text", {"text": "answer"}, 13),
        _event(
            "step_finish", {"tokens": {"total": 30, "input": 20, "output": 10}, "cost": 0.5}, 14
        ),
        "not json at all",
        json.dumps([1, 2]),
        json.dumps({"no_type": True}),
    ]
    tr = Transcript().parse("\n".join(lines))
    assert tr.events == 5 and len(tr.tools) == 2 and tr.tool_errors == 1
    assert tr.text.strip() == "answer" and tr.steps == 1
    assert (tr.tokens_total, tr.tokens_in, tr.tokens_out, tr.cost) == (30, 20, 10, 0.5)
    assert (tr.first_ts, tr.last_ts) == (10, 14) and not tr.silent


def test_silent_means_no_tool_and_no_text() -> None:
    tr = Transcript().parse(_event("step_start", {}) + "\n" + _event("step_finish", {"tokens": {}}))
    assert tr.silent and tr.events == 2
    assert Transcript().silent
