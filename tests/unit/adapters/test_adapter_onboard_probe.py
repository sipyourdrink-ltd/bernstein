"""Tests for the CLI probe evidence capture (issue #3762).

Covers the three evidence shapes: a deterministic happy-path probe, a probe
whose ``--version`` exits non-zero, and a binary absent from ``PATH``. All
three must produce a content-addressed evidence file and never raise.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.adapters.onboarding import probe_cli

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "probe"


def _read_evidence(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_deterministic_hash(tmp_path: Path) -> None:
    """Probing the same fixture twice yields identical content hashes."""
    binary = str(FIXTURES / "probe_ok.py")

    first = probe_cli(binary, tmp_path / "a")
    second = probe_cli(binary, tmp_path / "b")

    assert [e.sha256 for e in first] == [e.sha256 for e in second]
    # The version evidence is present, non-empty, and self-describing.
    version_ev = first[0]
    assert version_ev.path.is_file()
    doc = _read_evidence(version_ev.path)
    assert doc["exit_code"] == 0
    assert "probe-fixture 1.2.3" in doc["output"]


def test_failed_probe_evidence(tmp_path: Path) -> None:
    """A non-zero ``--version`` exit is recorded, not raised."""
    binary = str(FIXTURES / "probe_fail_version.py")

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
