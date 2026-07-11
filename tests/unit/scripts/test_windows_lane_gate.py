"""Unit tests for the self-promoting Windows-lane gate in
``scripts/windows_lane_gate.py``.

The Windows test lane runs today with a blanket ``continue-on-error: true``
mask: a red Windows step is silently swallowed forever, so the lane can
never block a merge even after it has proven green. That is a required
check with a permanent hole.

This gate replaces the blanket mask with a deterministic projection of
``(baseline_established, result_code)`` onto a blocking decision:

- No recorded green history yet -> the lane is advisory (a red result is
  surfaced as a warning but does not block). This is the
  ``non-blocking-if-no-history`` branch: a lane with no established
  baseline must not wedge the queue on its first red.
- A recorded green baseline -> the lane is gated (a red result blocks the
  merge). Once the lane has earned trust, regressions are hard failures.
- A green result always passes, regardless of baseline.

The decision is a pure function so it is verifiable offline and identical
on every host; the tests pin that projection and the CLI wiring.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "windows_lane_gate.py"


@pytest.fixture
def gate() -> Generator[ModuleType, None, None]:
    """Load scripts/windows_lane_gate.py as an importable module."""
    spec = importlib.util.spec_from_file_location(
        "windows_lane_gate_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


# ---------------------------------------------------------------------------
# decide(): the pure projection
# ---------------------------------------------------------------------------


class TestDecide:
    def test_green_result_passes_without_history(self, gate: ModuleType) -> None:
        d = gate.decide(result_code=0, established=False)
        assert d.blocking is False
        assert d.exit_code == 0
        assert d.level == "pass"

    def test_green_result_passes_with_history(self, gate: ModuleType) -> None:
        d = gate.decide(result_code=0, established=True)
        assert d.blocking is False
        assert d.exit_code == 0
        assert d.level == "pass"

    def test_red_without_history_is_advisory(self, gate: ModuleType) -> None:
        d = gate.decide(result_code=1, established=False)
        assert d.blocking is False
        assert d.exit_code == 0
        assert d.level == "advisory"

    def test_red_with_history_blocks(self, gate: ModuleType) -> None:
        d = gate.decide(result_code=1, established=True)
        assert d.blocking is True
        assert d.exit_code == 1
        assert d.level == "blocked"

    def test_nonstandard_nonzero_result_treated_as_red(self, gate: ModuleType) -> None:
        d = gate.decide(result_code=42, established=True)
        assert d.blocking is True
        assert d.exit_code == 1

    def test_projection_is_deterministic_and_json_serialisable(self, gate: ModuleType) -> None:
        d1 = gate.decide(result_code=1, established=True)
        d2 = gate.decide(result_code=1, established=True)
        proj1 = d1.to_projection()
        proj2 = d2.to_projection()
        assert proj1 == proj2
        # Must round-trip through JSON so it can be recorded/verified offline.
        assert json.loads(json.dumps(proj1)) == proj1
        assert proj1["level"] == "blocked"
        assert proj1["result_code"] == 1
        assert proj1["established"] is True


# ---------------------------------------------------------------------------
# load_baseline(): reading the committed marker
# ---------------------------------------------------------------------------


class TestLoadBaseline:
    def test_missing_file_is_not_established(self, gate: ModuleType, tmp_path: Path) -> None:
        assert gate.load_baseline(tmp_path / "absent.json") is False

    def test_established_true(self, gate: ModuleType, tmp_path: Path) -> None:
        p = tmp_path / "baseline.json"
        p.write_text(json.dumps({"established": True, "note": "10-run streak"}))
        assert gate.load_baseline(p) is True

    def test_established_false(self, gate: ModuleType, tmp_path: Path) -> None:
        p = tmp_path / "baseline.json"
        p.write_text(json.dumps({"established": False}))
        assert gate.load_baseline(p) is False

    def test_malformed_json_is_not_established(self, gate: ModuleType, tmp_path: Path) -> None:
        p = tmp_path / "baseline.json"
        p.write_text("{ not json")
        assert gate.load_baseline(p) is False

    def test_directory_traversal_baseline_rejected(self, gate: ModuleType, tmp_path: Path) -> None:
        # A baseline path resolving outside the repo root is refused (returns
        # not-established) rather than opening an arbitrary file.
        outside = tmp_path / ".." / "etc-passwd-shaped"
        assert gate.load_baseline(outside, repo_root=tmp_path / "repo") is False


# ---------------------------------------------------------------------------
# main(): CLI wiring + exit codes
# ---------------------------------------------------------------------------


class TestMain:
    def test_green_exits_zero(self, gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = gate.main(["--result", "0", "--baseline", str(tmp_path / "absent.json")])
        assert rc == 0

    def test_red_no_history_exits_zero_with_warning(
        self, gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = gate.main(["--result", "1", "--baseline", str(tmp_path / "absent.json")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "::warning" in out

    def test_red_with_history_exits_one_with_error(
        self, gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({"established": True}))
        rc = gate.main(["--result", "1", "--baseline", str(baseline)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "::error" in out

    def test_main_emits_projection_line(
        self, gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = gate.main(["--result", "0", "--baseline", str(tmp_path / "absent.json")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "windows-lane-gate:" in out
