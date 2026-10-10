"""bench.modelinfo: what the local runtime says about itself, failing soft."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from bench import modelinfo


def _serve(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    body = json.dumps(payload).encode()
    monkeypatch.setattr(modelinfo, "_open", lambda *_a, **_k: io.BytesIO(body))


def test_a_models_digest_is_read_from_the_tags_listing_by_name_or_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch, {"models": [{"name": "a:1", "digest": "d1"}, {"model": "b:2", "digest": "d2"}]}
    )
    assert modelinfo.model_digest("a:1") == "d1" and modelinfo.model_digest("b:2") == "d2"
    assert modelinfo.model_digest("c:3") == ""


def test_everything_fails_soft_to_an_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*_a: object, **_k: object) -> None:
        raise OSError("no ollama")

    monkeypatch.setattr(modelinfo, "_open", down)
    assert modelinfo.model_digest("m") == "" and modelinfo.model_parameters("m") == ""
    _serve(monkeypatch, "not a dict")
    assert modelinfo.model_digest("m") == ""


def _ollama_ps(monkeypatch: pytest.MonkeyPatch, out: str) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")
    )


def test_gpu_residency_is_the_processor_column_of_the_models_own_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Aligned like real `ollama ps`: a CONTEXT column, and a second model loaded first.
    out = (
        "NAME           ID            SIZE     PROCESSOR          CONTEXT    UNTIL\n"
        "gpt-oss:20b    17052f91a42e  12 GB    100% GPU           32768      4 minutes from now\n"
        "qwen3.5:27b    7653528ba5cb  17 GB    18%/82% CPU/GPU    32768      4 minutes from now\n"
    )
    _ollama_ps(monkeypatch, out)
    assert modelinfo.gpu_residency("qwen3.5:27b") == "18%/82% CPU/GPU"
    assert modelinfo.gpu_residency("gpt-oss:20b") == "100% GPU"  # no context size in the value
    assert modelinfo.gpu_residency("qwen3.5:9b") == ""  # not loaded: unknown, not another row's
    assert modelinfo.gpu_residency("qwen3.5") == ""  # a name prefix is not the model


def test_gpu_residency_without_a_context_column_ends_at_until(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ollama_ps(monkeypatch, "NAME ID SIZE PROCESSOR UNTIL\nm:1 abc 5 GB 100% GPU 4 minutes\n")
    assert modelinfo.gpu_residency("m:1").startswith("100% GPU")


def test_gpu_residency_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    _ollama_ps(monkeypatch, "NAME\n")
    assert modelinfo.gpu_residency("m:1") == ""
    _ollama_ps(monkeypatch, "")
    assert modelinfo.gpu_residency("m:1") == ""

    def missing(*_a: object, **_k: object) -> None:
        raise OSError("no ollama binary")

    monkeypatch.setattr(subprocess, "run", missing)
    assert modelinfo.gpu_residency("m:1") == ""


def test_the_opencode_version_is_empty_without_a_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    modelinfo.opencode_version.cache_clear()
    monkeypatch.setattr(modelinfo, "OPENCODE", tmp_path / "absent")
    assert modelinfo.opencode_version() == ""
    modelinfo.opencode_version.cache_clear()


def test_a_bare_model_name_finds_the_digest_of_its_latest_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch,
        {"models": [{"name": "m:latest", "digest": "dl"}, {"name": "m:7b", "digest": "d7"}]},
    )
    assert modelinfo.model_digest("m") == "dl" and modelinfo.model_digest("m:7b") == "d7"
