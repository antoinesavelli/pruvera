"""bench.modelinfo: what the local runtime says about itself, failing soft."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.request
from pathlib import Path

import pytest

from bench import modelinfo


def _serve(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    """Patches modelinfo._open to return the given payload as a JSON response body."""
    body = json.dumps(payload).encode()
    monkeypatch.setattr(modelinfo, "_open", lambda *_a, **_k: io.BytesIO(body))


def test_a_models_digest_is_read_from_the_tags_listing_by_name_or_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest is found by name or model key in the tags list; an unlisted model gives empty."""
    _serve(
        monkeypatch, {"models": [{"name": "a:1", "digest": "d1"}, {"model": "b:2", "digest": "d2"}]}
    )
    assert modelinfo.model_digest("a:1") == "d1" and modelinfo.model_digest("b:2") == "d2"
    assert modelinfo.model_digest("c:3") == ""


def test_everything_fails_soft_to_an_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Digest and parameter lookups return an empty string when Ollama is down or replies oddly."""

    def down(*_a: object, **_k: object) -> None:
        """Stands in for the Ollama request and raises OSError."""
        raise OSError("no ollama")

    monkeypatch.setattr(modelinfo, "_open", down)
    assert modelinfo.model_digest("m") == "" and modelinfo.model_parameters("m") == ""
    _serve(monkeypatch, "not a dict")
    assert modelinfo.model_digest("m") == ""


def _ollama_ps(monkeypatch: pytest.MonkeyPatch, out: str) -> None:
    """Patches subprocess.run to return the given text as the output of ollama ps."""
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")
    )


def test_gpu_residency_is_the_processor_column_of_the_models_own_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Aligned like real `ollama ps`: a CONTEXT column, and a second model loaded first.
    """Residency is the PROCESSOR cell of the exact model's row, empty for other or prefix names."""
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
    """Without a CONTEXT column the processor value still ends before the UNTIL column."""
    _ollama_ps(monkeypatch, "NAME ID SIZE PROCESSOR UNTIL\nm:1 abc 5 GB 100% GPU 4 minutes\n")
    assert modelinfo.gpu_residency("m:1").startswith("100% GPU")


def test_gpu_residency_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Residency is empty for a header-only or empty listing and for a missing ollama binary."""
    _ollama_ps(monkeypatch, "NAME\n")
    assert modelinfo.gpu_residency("m:1") == ""
    _ollama_ps(monkeypatch, "")
    assert modelinfo.gpu_residency("m:1") == ""

    def missing(*_a: object, **_k: object) -> None:
        """Stands in for subprocess.run and raises OSError for a missing binary."""
        raise OSError("no ollama binary")

    monkeypatch.setattr(subprocess, "run", missing)
    assert modelinfo.gpu_residency("m:1") == ""


def test_loaded_models_maps_each_listed_name_to_its_processor_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each loaded model maps to its own PROCESSOR cell, the same value gpu_residency reads."""
    out = (
        "NAME           ID            SIZE     PROCESSOR          CONTEXT    UNTIL\n"
        "gpt-oss:20b    17052f91a42e  12 GB    100% GPU           32768      4 minutes from now\n"
        "qwen3.5:27b    7653528ba5cb  17 GB    18%/82% CPU/GPU    32768      4 minutes from now\n"
    )
    _ollama_ps(monkeypatch, out)
    loaded = modelinfo.loaded_models()
    assert loaded == {"gpt-oss:20b": "100% GPU", "qwen3.5:27b": "18%/82% CPU/GPU"}
    assert all(modelinfo.gpu_residency(name) == cell for name, cell in loaded.items())


def test_a_repeated_model_name_keeps_its_first_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a name is listed twice, both lookups report the first row's PROCESSOR cell."""
    _ollama_ps(monkeypatch, "NAME  PROCESSOR\nm:1   100% GPU\nm:1   100% CPU\n")
    assert modelinfo.loaded_models() == {"m:1": "100% GPU"}
    assert modelinfo.gpu_residency("m:1") == "100% GPU"


def test_loaded_models_fails_soft_to_an_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """No models are reported for an empty or header-only listing or a missing ollama binary."""
    for out in ("", "NAME\n", "NAME  ID  SIZE  PROCESSOR  UNTIL\n"):
        _ollama_ps(monkeypatch, out)
        assert modelinfo.loaded_models() == {}

    def missing(*_a: object, **_k: object) -> None:
        """Stands in for subprocess.run and raises OSError for a missing binary."""
        raise OSError("no ollama binary")

    monkeypatch.setattr(subprocess, "run", missing)
    assert modelinfo.loaded_models() == {}


def test_post_json_posts_the_body_as_json_and_returns_the_reply_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body goes out as a JSON POST to the given path and port; the reply object comes back."""
    sent: list[tuple[urllib.request.Request, float]] = []

    def fake_open(req: urllib.request.Request, timeout: float) -> io.BytesIO:
        """Records the request and its timeout and replies with a JSON object."""
        sent.append((req, timeout))
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(modelinfo, "_open", fake_open)
    assert modelinfo.post_json("/api/show", {"model": "m"}, 5) == {"ok": True}
    assert modelinfo.post_json("/api/ps", {}, 2, port=1234) == {"ok": True}
    (req, timeout), (other, _) = sent
    assert req.get_full_url() == "http://127.0.0.1:11434/api/show" and timeout == 5
    assert req.get_method() == "POST" and req.get_header("Content-type") == "application/json"
    assert isinstance(req.data, bytes) and json.loads(req.data) == {"model": "m"}
    assert other.get_full_url() == "http://127.0.0.1:1234/api/ps"


def test_post_json_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply that is not a JSON object raises ValueError; an OSError from Ollama propagates."""
    _serve(monkeypatch, ["not", "an", "object"])
    with pytest.raises(ValueError, match="not a JSON object"):
        modelinfo.post_json("/api/show", {}, 5)

    def down(*_a: object, **_k: object) -> None:
        """Stands in for the Ollama request and raises OSError."""
        raise OSError("no ollama")

    monkeypatch.setattr(modelinfo, "_open", down)
    with pytest.raises(OSError, match="no ollama"):
        modelinfo.post_json("/api/show", {}, 5)


def test_model_parameters_are_the_show_replys_parameters_on_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /api/show parameters come back whitespace-collapsed; a non-object reply gives empty."""
    _serve(monkeypatch, {"parameters": "temperature 1\ntop_p       0.95"})
    assert modelinfo.model_parameters("m") == "temperature 1 top_p 0.95"
    _serve(monkeypatch, ["not", "an", "object"])
    assert modelinfo.model_parameters("m") == ""


def test_the_opencode_version_is_empty_without_a_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The opencode version is empty when the binary path does not exist."""
    modelinfo.opencode_version.cache_clear()
    monkeypatch.setattr(modelinfo, "OPENCODE", tmp_path / "absent")
    assert modelinfo.opencode_version() == ""
    modelinfo.opencode_version.cache_clear()


def test_a_bare_model_name_finds_the_digest_of_its_latest_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare model name resolves to the digest of its latest tag, an explicit tag to its own."""
    _serve(
        monkeypatch,
        {"models": [{"name": "m:latest", "digest": "dl"}, {"name": "m:7b", "digest": "d7"}]},
    )
    assert modelinfo.model_digest("m") == "dl" and modelinfo.model_digest("m:7b") == "d7"
