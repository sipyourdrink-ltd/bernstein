"""Tests for config provenance features — T519, T557, T558, T559.

Covers:
- Allowed setting source policy enforcement (T519)
- Setting conflict explainer (T559)
- Settings snapshot capture (T557)
- Session-stable flag latching registry (T558)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.home import (
    BernsteinHome,
    SettingsSnapshot,
    capture_settings_snapshot,
    check_source_policies,
    enforce_source_policy,
    explain_conflicts,
    resolve_config,
    resolve_config_bundle,
)
from bernstein.core.session import latch_session_flags, load_latched_flags


# ---------------------------------------------------------------------------
# T519 — Allowed setting source policy enforcement
# ---------------------------------------------------------------------------


class TestEnforceSourcePolicy:
    def test_no_policy_for_key_returns_none(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        result = resolve_config("cli", home=home, project_dir=tmp_path)
        violation = enforce_source_policy("cli", result)
        assert violation is None  # no policy for "cli"

    def test_policy_violation_when_source_disallowed(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        result = resolve_config(
            "budget",
            home=home,
            project_dir=tmp_path,
            session_overrides={"budget": "5.0"},
        )
        # budget policy disallows "session" source
        violation = enforce_source_policy("budget", result)
        assert violation is not None
        assert violation["key"] == "budget"
        assert violation["actual_source"] == "session"
        assert "session" not in violation["allowed_sources"]

    def test_no_violation_when_source_allowed(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("budget", 10.0)
        result = resolve_config("budget", home=home, project_dir=tmp_path)
        violation = enforce_source_policy("budget", result)
        assert violation is None  # "global" is allowed for budget

    def test_extra_policies_override_defaults(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        result = resolve_config("cli", home=home, project_dir=tmp_path)
        # Add a custom policy that only allows "project" for "cli"
        violation = enforce_source_policy("cli", result, extra_policies={"cli": ("project",)})
        assert violation is not None
        assert violation["actual_source"] == "default"

    def test_check_source_policies_returns_all_violations(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        bundle = resolve_config_bundle(
            home=home,
            project_dir=tmp_path,
            session_overrides={"budget": "5.0", "max_agents": "3"},
        )
        violations = check_source_policies(bundle)
        violated_keys = {v["key"] for v in violations}
        assert "budget" in violated_keys
        assert "max_agents" in violated_keys

    def test_violation_message_is_human_readable(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        result = resolve_config(
            "budget",
            home=home,
            project_dir=tmp_path,
            session_overrides={"budget": "5.0"},
        )
        violation = enforce_source_policy("budget", result)
        assert violation is not None
        assert "budget" in violation["message"]
        assert "session" in violation["message"]


# ---------------------------------------------------------------------------
# T559 — Setting conflict explainer
# ---------------------------------------------------------------------------


class TestExplainConflicts:
    def test_no_conflicts_when_single_source(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        bundle = resolve_config_bundle(home=home, project_dir=tmp_path)
        conflicts = explain_conflicts(bundle)
        assert conflicts == []

    def test_detects_conflict_between_global_and_project(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("cli", "codex")
        sdd_config = tmp_path / ".sdd" / "config.yaml"
        sdd_config.parent.mkdir(parents=True)
        sdd_config.write_text("cli: gemini\n")

        bundle = resolve_config_bundle(home=home, project_dir=tmp_path)
        conflicts = explain_conflicts(bundle)
        cli_conflicts = [c for c in conflicts if c["key"] == "cli"]
        assert len(cli_conflicts) == 1
        assert "codex" in cli_conflicts[0]["explanation"] or "gemini" in cli_conflicts[0]["explanation"]

    def test_conflict_explanation_names_winning_source(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("cli", "codex")
        sdd_config = tmp_path / ".sdd" / "config.yaml"
        sdd_config.parent.mkdir(parents=True)
        sdd_config.write_text("cli: gemini\n")

        bundle = resolve_config_bundle(home=home, project_dir=tmp_path)
        conflicts = explain_conflicts(bundle)
        cli_conflict = next(c for c in conflicts if c["key"] == "cli")
        assert cli_conflict["winning_source"] == "project"
        assert cli_conflict["winning_value"] == "gemini"


# ---------------------------------------------------------------------------
# T557 — Settings snapshot capture
# ---------------------------------------------------------------------------


class TestCaptureSettingsSnapshot:
    def test_snapshot_contains_all_default_keys(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        snapshot = capture_settings_snapshot(home=home, project_dir=tmp_path)
        assert isinstance(snapshot, dict)
        assert "settings" in snapshot
        assert "sources" in snapshot
        assert "captured_at" in snapshot

    def test_snapshot_records_project_dir(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        snapshot = capture_settings_snapshot(home=home, project_dir=tmp_path)
        assert snapshot["project_dir"] == str(tmp_path)

    def test_snapshot_includes_conflicts(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        home.set("cli", "codex")
        sdd_config = tmp_path / ".sdd" / "config.yaml"
        sdd_config.parent.mkdir(parents=True)
        sdd_config.write_text("cli: gemini\n")

        snapshot = capture_settings_snapshot(home=home, project_dir=tmp_path)
        assert len(snapshot["conflicts"]) >= 1

    def test_snapshot_includes_policy_violations(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        snapshot = capture_settings_snapshot(
            home=home,
            project_dir=tmp_path,
            session_overrides={"budget": "5.0"},
        )
        assert len(snapshot["policy_violations"]) >= 1


# ---------------------------------------------------------------------------
# T558 — Session-stable flag latching registry
# ---------------------------------------------------------------------------


class TestLatchedFlags:
    def test_latch_and_load_roundtrip(self, tmp_path: Path) -> None:
        flags = {"feature_x": True, "feature_y": False, "max_retries": 3}
        latch_session_flags(tmp_path, flags)
        loaded = load_latched_flags(tmp_path)
        assert loaded["feature_x"] is True
        assert loaded["feature_y"] is False
        assert loaded["max_retries"] == 3

    def test_load_returns_empty_when_no_latch_file(self, tmp_path: Path) -> None:
        loaded = load_latched_flags(tmp_path)
        assert loaded == {}

    def test_latch_creates_parent_dirs(self, tmp_path: Path) -> None:
        latch_session_flags(tmp_path, {"x": 1})
        latch_path = tmp_path / ".sdd" / "runtime" / "latched_flags.json"
        assert latch_path.exists()

    def test_latch_overwrites_previous(self, tmp_path: Path) -> None:
        latch_session_flags(tmp_path, {"a": 1})
        latch_session_flags(tmp_path, {"b": 2})
        loaded = load_latched_flags(tmp_path)
        assert "a" not in loaded
        assert loaded["b"] == 2
