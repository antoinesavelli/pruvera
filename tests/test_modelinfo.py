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


def test_gpu_residency_is_the_processor_column_of_ollama_ps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = "NAME ID SIZE PROCESSOR UNTIL\nm:1 abc 5 GB 100% GPU 4 minutes\n"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")
    )
    assert modelinfo.gpu_residency().startswith("100% GPU")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "NAME\n", "")
    )
    assert modelinfo.gpu_residency() == ""

    def missing(*_a: object, **_k: object) -> None:
        raise OSError("no ollama binary")

    monkeypatch.setattr(subprocess, "run", missing)
    assert modelinfo.gpu_residency() == ""


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
