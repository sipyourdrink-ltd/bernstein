"""Unit tests for #3145: Moving bernstein listen to optional voice extra."""

from __future__ import annotations

import builtins
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.voice_cmd import _import_audio_deps
from bernstein.cli.main import cli

_INSTALL_HINT = "pip install 'bernstein[voice]'"


@pytest.fixture
def block_imports(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Force ``ImportError`` for named top-level modules.

    The voice code paths are reached only when the ``voice`` extra is absent.
    Relying on the ambient environment makes the assertion accidental: on a
    machine that has ``bernstein[voice]`` installed the same test would build a
    real ``WhisperModel`` (a ~150 MB download) and then block on a live
    microphone stream. Blocking the import explicitly keeps the branch under
    test regardless of what is installed.
    """
    real_import = builtins.__import__

    def _block(*names: str, error: BaseException | None = None) -> None:
        blocked = frozenset(names)

        def fake_import(
            name: str,
            globals: dict[str, Any] | None = None,
            locals: dict[str, Any] | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> Any:
            root = name.partition(".")[0]
            if root in blocked:
                raise error if error is not None else ImportError(f"No module named {root!r}")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)

    yield _block


def test_voice_optional_dependency_extra_defined() -> None:
    pyproject_path = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    extras = data.get("project", {}).get("optional-dependencies", {})
    assert "voice" in extras, "voice optional dependency extra missing from pyproject.toml"
    voice_deps = extras["voice"]
    assert any("faster-whisper" in dep for dep in voice_deps)
    assert any("sounddevice" in dep for dep in voice_deps)
    assert any("numpy" in dep for dep in voice_deps)


def test_listen_without_extra_gives_informative_error(block_imports: Any) -> None:
    """``bernstein listen`` names the extra when faster-whisper is missing."""
    block_imports("faster_whisper")

    res = CliRunner().invoke(cli, ["listen"])

    assert res.exit_code != 0
    assert _INSTALL_HINT in res.output
    assert "pip install faster-whisper" not in res.output


def test_import_audio_deps_without_extra_gives_informative_error(
    block_imports: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The audio-capture import path names the extra too, not the raw packages.

    ``_import_audio_deps`` is reached only after the whisper model loads, so the
    ``listen`` end-to-end test never exercises it. Without this test its error
    message can be reverted to the pre-#3145 wording with the suite still green.
    """
    block_imports("numpy", "sounddevice")

    with pytest.raises(SystemExit) as excinfo:
        _import_audio_deps()

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert _INSTALL_HINT in out
    assert "pip install sounddevice numpy" not in out


def test_missing_portaudio_is_not_reported_as_a_missing_extra(
    block_imports: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PortAudio load failure gets its own instruction, not "install the extra".

    ``sounddevice`` dlopens PortAudio at import time. With the wheel installed
    but the system library absent - the documented Linux case - it raises
    ``OSError``, which the ImportError handler does not catch, so the operator
    got a raw traceback after already waiting through a ~150 MB model download.
    """
    block_imports("sounddevice", error=OSError("PortAudio library not found"))

    with pytest.raises(SystemExit) as excinfo:
        _import_audio_deps()

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "PortAudio" in out
    assert _INSTALL_HINT not in out, "installing the extra does not fix a missing system library"


def test_listen_help_documents_the_voice_extra() -> None:
    """``bernstein listen --help`` points at the extra, not the raw packages."""
    res = CliRunner().invoke(cli, ["listen", "--help"])

    assert res.exit_code == 0
    assert _INSTALL_HINT in res.output
    assert "pip install faster-whisper sounddevice numpy" not in res.output
