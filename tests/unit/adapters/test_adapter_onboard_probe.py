"""Tests for the CLI probe evidence capture (issue #3762).

Covers the three evidence shapes: a deterministic happy-path probe, a probe
whose ``--version`` exits non-zero, and a binary absent from ``PATH``. All
three must produce a content-addressed evidence file and never raise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bernstein.adapters import onboarding
from bernstein.adapters.onboarding import probe_cli

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "probe"


def _read_evidence(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_python_fixture(monkeypatch: pytest.MonkeyPatch, fixture: Path) -> None:
    """Execute one Python fixture without changing the logical probe command."""
    run_capture = onboarding._run_capture

    def run_fixture(cmd: list[str], *, timeout: int, env: dict[str, str] | None = None) -> tuple[int, str]:
        assert cmd[0] == str(fixture)
        return run_capture([sys.executable, *cmd], timeout=timeout, env=env)

    monkeypatch.setattr(onboarding, "_run_capture", run_fixture)


def test_deterministic_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Probing the same fixture twice yields identical content hashes."""
    fixture = FIXTURES / "probe_ok.py"
    binary = str(fixture)
    _run_python_fixture(monkeypatch, fixture)

    first = probe_cli(binary, tmp_path / "a")
    second = probe_cli(binary, tmp_path / "b")

    assert [e.sha256 for e in first] == [e.sha256 for e in second]
    # The version evidence is present, non-empty, and self-describing.
    version_ev = first[0]
    assert version_ev.path.is_file()
    doc = _read_evidence(version_ev.path)
    assert doc["binary"] == binary
    assert doc["command"] == f"{binary} --version"
    assert doc["exit_code"] == 0
    assert "probe-fixture 1.2.3" in doc["output"]


def test_failed_probe_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero ``--version`` exit is recorded, not raised."""
    fixture = FIXTURES / "probe_fail_version.py"
    binary = str(fixture)
    _run_python_fixture(monkeypatch, fixture)

    evidence = probe_cli(binary, tmp_path)

    version_ev = evidence[0]
    assert version_ev.path.is_file()
    doc = _read_evidence(version_ev.path)
    assert doc["exit_code"] == 2
    assert "version check failed" in doc["output"]


def test_missing_binary_evidence(tmp_path: Path) -> None:
    """A binary absent from PATH yields evidence naming the failure, no raise."""
    evidence = probe_cli("definitely-not-a-real-binary-xyz", tmp_path)

    version_ev = evidence[0]
    assert version_ev.path.is_file()
    doc = _read_evidence(version_ev.path)
    assert doc["exit_code"] == 127
    assert "not found in PATH" in doc["output"]
