"""Parse `opencode run --format json` events into what a trial record needs (no scoring).

Depends on: stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MAX_TOOLS = 20_000  # events kept per trial: a flood of events must not grow the harness's memory
MAX_TEXT = 1_000_000  # characters of accumulated assistant text kept
MAX_INPUT = 20_000  # characters of one tool call's arguments kept


def _num(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bounded_input(value: object) -> object:
    text = json.dumps(value, default=str)
    return value if len(text) <= MAX_INPUT else {"truncated": text[:MAX_INPUT]}


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
    tool_count: int = 0
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
        part = _dict(event.get("part"))
        kind = event["type"]
        if kind == "tool_use":
            state = _dict(part.get("state"))
            self.tool_errors += state.get("status") == "error"
            self.tool_count += 1
            if len(self.tools) < MAX_TOOLS:
                self.tools.append(
                    {
                        "tool": part.get("tool"),
                        "input": _bounded_input(state.get("input", {})),
                        "status": state.get("status"),
                        "output": str(state.get("output", ""))[:400],
                        "error": str(state.get("error", ""))[:300],
                    }
                )
        elif kind == "text":
            body = str(part.get("text", ""))
            if len(self.text) < MAX_TEXT:
                self.text += body[:MAX_TEXT] + "\n"
            self.final = body[:MAX_TEXT]
        elif kind == "step_finish":
            self.steps += 1
            tokens = _dict(part.get("tokens"))
            self.tokens_total += int(_num(tokens.get("total")))
            self.tokens_in += int(_num(tokens.get("input")))
            self.tokens_out += int(_num(tokens.get("output")))
            self.cost += _num(part.get("cost"))
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


OUTPUT_KEEP = 120  # characters of a tool's output kept from a reference trial
TEXT_KEEP = 300  # and of an assistant message
INPUT_KEEP = 200  # and of each string in a tool call's arguments
EVENT_FIELDS = ("type", "timestamp")
PART_FIELDS = ("type", "tool", "reason", "tokens", "cost")


def _shorten(value: object, keep: int) -> object:
    if isinstance(value, str):
        return value[:keep]
    if isinstance(value, dict):
        return {str(k)[:keep]: _shorten(v, keep) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [_shorten(v, keep) for v in value[:50]]
    return value


def _sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    part = _dict(event.get("part"))
    kept_part = {k: part[k] for k in PART_FIELDS if k in part}
    if "text" in part:
        kept_part["text"] = str(part["text"])[:TEXT_KEEP]
    state = _dict(part.get("state"))
    if state:
        kept_part["state"] = {
            "status": state.get("status"),
            "input": _shorten(state.get("input", {}), INPUT_KEEP),
            "output": str(state.get("output", ""))[:OUTPUT_KEEP],
            "error": str(state.get("error", ""))[:OUTPUT_KEEP],
        }
    return {**{k: event[k] for k in EVENT_FIELDS if k in event}, "part": kept_part}


def sanitize_transcript(raw: str) -> str:
    """A reference transcript cut down to a whitelist of fields, each with a few characters."""
    # The copy ran on real strategy code, so a full transcript is real code. What the comparison
    # needs is which tools ran, with what input, how they ended, and the short error text; any
    # field not named here (tool metadata, raw arguments, provider payloads) is dropped.
    kept = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            kept.append(line[:TEXT_KEEP])  # not an event: keep a short stub, never the body
            continue
        kept.append(json.dumps(_sanitize_event(event) if isinstance(event, dict) else line[:80]))
    return "\n".join(kept) + "\n"
