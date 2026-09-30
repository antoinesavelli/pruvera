"""Parse `opencode run --format json` events into what a trial record needs (no scoring).

Depends on: stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Transcript:
    events: int = 0
    tools: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    final: str = ""  # the last text part: what the agent left as its answer
    steps: int = 0
    tokens_total: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    tool_errors: int = 0
    first_ts: int | None = None
    last_ts: int | None = None

    def feed(self, line: str) -> bool:
        """Add one raw stdout line; True if it was a well-formed event."""
        try:
            event = json.loads(line)
        except ValueError:
            return False
        if not isinstance(event, dict) or "type" not in event:
            return False
        self.events += 1
        ts = event.get("timestamp")
        if isinstance(ts, int):
            self.first_ts = self.first_ts if self.first_ts is not None else ts
            self.last_ts = ts
        part = event.get("part", {})
        kind = event["type"]
        if kind == "tool_use":
            state = part.get("state", {})
            self.tools.append(
                {
                    "tool": part.get("tool"),
                    "input": state.get("input", {}),
                    "status": state.get("status"),
                    "output": str(state.get("output", ""))[:400],
                    "error": str(state.get("error", ""))[:300],
                }
            )
            self.tool_errors += state.get("status") == "error"
        elif kind == "text":
            self.text += part.get("text", "") + "\n"
            self.final = str(part.get("text", ""))
        elif kind == "step_finish":
            self.steps += 1
            tokens = part.get("tokens", {})
            self.tokens_total += int(tokens.get("total", 0))
            self.tokens_in += int(tokens.get("input", 0))
            self.tokens_out += int(tokens.get("output", 0))
            self.cost += float(part.get("cost", 0) or 0)
        return True

    def parse(self, raw: str) -> Transcript:
        for line in raw.splitlines():
            self.feed(line)
        return self

    @property
    def silent(self) -> bool:
        """No tool call and no text: the agent produced nothing an observer could see."""
        return not self.tools and not self.text.strip()

    @property
    def answer_kind(self) -> str:
        """`text`, `empty` (no final text) or `tool_json` (a raw tool call shown as the answer)."""
        return answer_kind(self.final)


def answer_kind(text: str) -> str:
    """Classify a final answer: `text`, `empty`, or `tool_json` (a tool call printed as text)."""
    body = text.strip()
    if not body:
        return "empty"
    if body.startswith("{") and any(f'"{k}"' in body[:80] for k in ("name", "tool", "arguments")):
        return "tool_json"
    return "text"
