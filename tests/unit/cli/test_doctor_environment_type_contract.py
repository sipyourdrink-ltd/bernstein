"""Doctor accepts optional environment snapshots without losing PATH semantics."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import pytest

from bernstein.cli.commands.status_cmd import _doctor_check_adapters


def test_doctor_accepts_a_typed_optional_environment_snapshot(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    caller = tmp_path / "doctor_caller.py"
    caller.write_text(
        "from collections.abc import Mapping\n"
        "from bernstein.cli.commands.status_cmd import _doctor_check_adapters\n"
        "def inspect_environment(environment: Mapping[str, str] | None) -> bool:\n"
        "    return _doctor_check_adapters([], env=environment)\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--config-file",
        str(root / "pyproject.toml"),
        "--follow-imports=silent",
        "--cache-dir",
        str(tmp_path / "mypy-cache"),
        str(root / "src/bernstein/cli/commands/status_cmd.py"),
        str(caller),
    ]
    checked = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=120, check=False)
    assert checked.returncode == 0, checked.stdout + checked.stderr

    with caller.open("a", encoding="utf-8") as stream:
        stream.write("_doctor_check_adapters([], env=1)\n")
    rejected = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=120, check=False)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert "[arg-type]" in rejected.stdout
    assert 'Argument "env" to "_doctor_check_adapters"' in rejected.stdout


@pytest.mark.parametrize(
    ("environment", "lookup_options"),
    [
        (None, {}),
        ({}, {}),
        (MappingProxyType({"PATH": ""}), {"path": ""}),
        (MappingProxyType({"PATH": "/captured/bin"}), {"path": "/captured/bin"}),
    ],
    ids=["ambient", "missing-path", "empty-path", "captured-path"],
)
def test_doctor_preserves_explicit_path_and_ambient_fallback(
    environment: Mapping[str, str] | None,
    lookup_options: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def lookup(command: str, **options: str) -> None:
        calls.append((command, options))

    monkeypatch.setattr(shutil, "which", lookup)

    checks: list[dict] = []
    assert _doctor_check_adapters(checks, env=environment) is False
    assert calls == [(name, lookup_options) for name in ("claude", "codex", "gemini")]
    assert len(checks) == 3
