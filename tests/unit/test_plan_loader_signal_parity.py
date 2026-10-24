"""Regression tests: plan loader preserves all supported completion signal types (#6646).

Before the fix, _parse_completion_signals had a hardcoded valid_types set of
six entries.  Artifact-mode signals (schema_valid, criteria_match, hash_stable)
and newer types (figures_grounded, absence_verified) were silently dropped:
the task reached the server with no gate, and the janitor could never verify it.

These tests verify:
- schema_valid / criteria_match / hash_stable survive load_plan unchanged.
- figures_grounded and absence_verified survive.
- Unknown signal types now raise PlanLoadError (fail-closed).
- Non-mapping entries in completion_signals raise PlanLoadError.
- Empty-value entries raise PlanLoadError.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.planning.plan_loader import PlanLoadError, load_plan
from bernstein.core.planning.plan_schema import SUPPORTED_SIGNAL_TYPES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plan(tmp_path: Path, signals: list[dict]) -> Path:
    """Write a minimal single-step plan with the given completion_signals."""
    import yaml

    plan = {
        "name": "test-plan",
        "stages": [
            {
                "name": "stage1",
                "steps": [
                    {
                        "title": "Do the work",
                        "role": "backend",
                        "completion_signals": signals,
                    }
                ],
            }
        ],
    }
    p = tmp_path / "plan.yaml"
    p.write_text(yaml.dump(plan), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Tests: artifact signals survive
# ---------------------------------------------------------------------------


class TestArtifactSignalsParity:
    """Artifact-mode completion signals survive into the loaded task (#6646)."""

    def test_schema_valid_preserved(self, tmp_path: Path) -> None:
        schema = '{"type": "object", "required": ["ok"]}'
        p = _write_plan(tmp_path, [{"type": "schema_valid", "value": schema}])
        _, tasks = load_plan(p)
        sigs = tasks[0].completion_signals
        assert len(sigs) == 1
        assert sigs[0].type == "schema_valid"
        assert sigs[0].value == schema

    def test_criteria_match_preserved(self, tmp_path: Path) -> None:
        criteria = '{"field": "status", "op": "eq", "value": "ok"}'
        p = _write_plan(tmp_path, [{"type": "criteria_match", "value": criteria}])
        _, tasks = load_plan(p)
        sigs = tasks[0].completion_signals
        assert len(sigs) == 1
        assert sigs[0].type == "criteria_match"
        assert sigs[0].value == criteria

    def test_hash_stable_preserved(self, tmp_path: Path) -> None:
        expected_hash = "sha256:abc123"
        p = _write_plan(tmp_path, [{"type": "hash_stable", "value": expected_hash}])
        _, tasks = load_plan(p)
        sigs = tasks[0].completion_signals
        assert len(sigs) == 1
        assert sigs[0].type == "hash_stable"
        assert sigs[0].value == expected_hash

    def test_figures_grounded_preserved(self, tmp_path: Path) -> None:
        p = _write_plan(tmp_path, [{"type": "figures_grounded", "value": "report.json"}])
        _, tasks = load_plan(p)
        sigs = tasks[0].completion_signals
        assert len(sigs) == 1
        assert sigs[0].type == "figures_grounded"

    def test_absence_verified_preserved(self, tmp_path: Path) -> None:
        p = _write_plan(tmp_path, [{"type": "absence_verified", "value": "search-corpus"}])
        _, tasks = load_plan(p)
        sigs = tasks[0].completion_signals
        assert len(sigs) == 1
        assert sigs[0].type == "absence_verified"

    def test_mixed_basic_and_artifact_signals_both_preserved(self, tmp_path: Path) -> None:
        p = _write_plan(
            tmp_path,
            [
                {"type": "test_passes", "command": "pytest"},
                {"type": "schema_valid", "value": '{"type": "object"}'},
            ],
        )
        _, tasks = load_plan(p)
        sigs = tasks[0].completion_signals
        assert len(sigs) == 2
        assert {s.type for s in sigs} == {"test_passes", "schema_valid"}


# ---------------------------------------------------------------------------
# Tests: fail-closed on malformed entries
# ---------------------------------------------------------------------------


class TestFailClosed:
    """Unknown / malformed signal entries raise PlanLoadError (fail-closed)."""

    def test_unknown_type_raises(self, tmp_path: Path) -> None:
        p = _write_plan(tmp_path, [{"type": "invented_signal", "value": "x"}])
        with pytest.raises(PlanLoadError, match="unknown type"):
            load_plan(p)

    def test_empty_type_raises(self, tmp_path: Path) -> None:
        p = _write_plan(tmp_path, [{"value": "x"}])
        with pytest.raises(PlanLoadError, match="unknown type"):
            load_plan(p)

    def test_non_mapping_entry_raises(self, tmp_path: Path) -> None:

        plan_text = """
name: bad-plan
stages:
  - name: s1
    steps:
      - title: step
        role: backend
        completion_signals:
          - just_a_string
"""
        p = tmp_path / "plan.yaml"
        p.write_text(plan_text, encoding="utf-8")
        with pytest.raises(PlanLoadError, match="not a mapping"):
            load_plan(p)

    def test_empty_value_raises(self, tmp_path: Path) -> None:
        p = _write_plan(tmp_path, [{"type": "test_passes"}])
        with pytest.raises(PlanLoadError, match="no value"):
            load_plan(p)

    def test_loader_accepts_exactly_supported_signal_types(self, tmp_path: Path) -> None:
        """The loader's accepted set must equal SUPPORTED_SIGNAL_TYPES in plan_schema.

        Adding a type to plan_schema.SUPPORTED_SIGNAL_TYPES must be sufficient to
        make it loadable — no separate hand-edit of the loader required.
        """
        # Verify every type in SUPPORTED_SIGNAL_TYPES loads successfully.
        # Use a generic value that satisfies all non-file_contains signal types.
        for sig_type in sorted(SUPPORTED_SIGNAL_TYPES):
            if sig_type == "file_contains":
                sig = {"type": sig_type, "path": "README.md", "contains": "hello"}
            else:
                sig = {"type": sig_type, "value": "dummy"}
            p = _write_plan(tmp_path, [sig])
            _, tasks = load_plan(p)
            signals = tasks[0].completion_signals
            assert any(s.type == sig_type for s in signals), (
                f"signal type {sig_type!r} from SUPPORTED_SIGNAL_TYPES was not preserved by load_plan"
            )
