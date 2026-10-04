"""Assemble a trial's opencode config: the real global config minus what must not reach it.

Every difference from the real file is a named deviation with a reason; `check_parity` fails on any
that is not. The project config, prompts, commands and AGENTS.md are never touched: they come from
the fixture tree at the real repo path, as in a real session. The model under test goes in through
OPENCODE_CONFIG_CONTENT (highest precedence), so no project file is edited.

Depends on: the real global config file (system-library); stdlib only.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REAL_GLOBAL = Path("/mnt/ParamoStorage/system-library/machines/workstation/opencode/opencode.json")


class ParityError(RuntimeError):
    """The assembled config differs from the real one in a way no deviation explains."""


@dataclass(frozen=True)
class Deviation:
    pointer: str  # JSON pointer of the changed location (prefix match)
    action: str  # "removed" or "changed"
    reason: str


@dataclass(frozen=True)
class Assembly:
    config: dict[str, Any]
    deviations: tuple[Deviation, ...]
    real_sha256: str
    inline: dict[str, Any]  # goes in OPENCODE_CONFIG_CONTENT: the model under test

    def env(self) -> dict[str, str]:
        return {"OPENCODE_CONFIG_CONTENT": json.dumps(self.inline, sort_keys=True)}


def diff_pointers(real: Any, other: Any, base: str = "") -> list[tuple[str, str]]:
    """(pointer, kind) for every place the two JSON values differ; kind is removed/added/changed."""
    if isinstance(real, dict) and isinstance(other, dict):
        out: list[tuple[str, str]] = []
        for key in sorted(set(real) | set(other)):
            pointer = f"{base}/{key}"
            if key not in other:
                out.append((pointer, "removed"))
            elif key not in real:
                out.append((pointer, "added"))
            else:
                out += diff_pointers(real[key], other[key], pointer)
        return out
    return [] if real == other else [(base, "changed")]


def check_parity(
    real: dict[str, Any], config: dict[str, Any], deviations: tuple[Deviation, ...]
) -> None:
    """Raise unless every difference between `real` and `config` lies under a declared deviation."""
    unexplained = [
        f"{kind} {pointer}"
        for pointer, kind in diff_pointers(real, config)
        if not any(pointer == d.pointer or pointer.startswith(d.pointer + "/") for d in deviations)
    ]
    if unexplained:
        raise ParityError("unexplained differences: " + "; ".join(unexplained))


SECRET_KEYS = frozenset({"apikey", "api_key", "token", "secret", "password"})


def _refuse_literal_secrets(node: Any, path: str = "") -> None:
    """A config the agent can read holds no credential value, only `{env:...}` references."""
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                key.lower() in SECRET_KEYS
                and isinstance(value, str)
                and not value.startswith("{env:")
            ):
                raise ParityError(f"{path}/{key}: a literal credential in the config a trial reads")
            _refuse_literal_secrets(value, f"{path}/{key}")


def assemble(agent: str, model: str, real_path: Path = REAL_GLOBAL) -> Assembly:
    """The global config for a trial: `agent` runs on local `model`; remote and MCP parts go."""
    raw = real_path.read_bytes()
    real: dict[str, Any] = json.loads(raw)
    config = copy.deepcopy(real)
    deviations: list[Deviation] = []
    for name in [n for n in config.get("provider", {}) if n != "ollama"]:  # an allowlist
        del config["provider"][name]
        deviations.append(
            Deviation(f"/provider/{name}", "removed", "remote provider: cost and data egress")
        )
    _refuse_literal_secrets(config)
    if "mcp" in config:
        del config["mcp"]
        deviations.append(
            Deviation("/mcp", "removed", "MCP servers reach real data (opencode 1.18.31 finding)")
        )
    for name, spec in config.get("agent", {}).items():
        if str(spec.get("model", "")).startswith("openrouter/"):
            spec["model"] = f"ollama/{model}"
            deviations.append(
                Deviation(
                    f"/agent/{name}/model", "changed", "remote model replaced by the local one"
                )
            )
    listed = config.setdefault("provider", {}).setdefault("ollama", {}).setdefault("models", {})
    if (
        model not in listed
    ):  # opencode fails with "Unexpected server error" on a model it does not list
        listed[model] = {"name": model}
        deviations.append(
            Deviation(
                f"/provider/ollama/models/{model}", "added", "the model under test is not listed"
            )
        )
    inline = {"agent": {agent: {"model": f"ollama/{model}"}}}
    return Assembly(config, tuple(deviations), hashlib.sha256(raw).hexdigest(), inline)


def write(assembly: Assembly, xdg_config: Path) -> Path:
    """Write the global config into the trial's XDG config dir; returns the file path."""
    target = xdg_config / "opencode" / "opencode.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(assembly.config, indent=2, sort_keys=True) + "\n")
    return target
