"""Tests for config source checksum watcher (config drift detection)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from bernstein.core.config_watcher import (
    ConfigWatcher,
    DriftReport,
    _hash_file,
    _severity_for_label,
    discover_config_paths,
)

# ---------------------------------------------------------------------------
# _hash_file
# ---------------------------------------------------------------------------


def test_hash_file_returns_sha256(tmp_path: Path) -> None:
    """_hash_file should return the SHA-256 hex digest of file contents."""
    f = tmp_path / "sample.yaml"
    content = b"cli: claude\nmax_agents: 4\n"
    f.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    assert _hash_file(f) == expected


def test_hash_file_missing_returns_empty(tmp_path: Path) -> None:
    """_hash_file should return empty string for non-existent files."""
    assert _hash_file(tmp_path / "does_not_exist.yaml") == ""


def test_hash_file_empty_file(tmp_path: Path) -> None:
    """_hash_file should return the hash of empty bytes for an empty file."""
    f = tmp_path / "empty.yaml"
    f.write_bytes(b"")
    expected = hashlib.sha256(b"").hexdigest()
    assert _hash_file(f) == expected


# ---------------------------------------------------------------------------
# _severity_for_label
# ---------------------------------------------------------------------------


def test_severity_for_project_label() -> None:
    assert _severity_for_label("project") == "warning"


def test_severity_for_user_label() -> None:
    assert _severity_for_label("user") == "warning"


def test_severity_for_local_label() -> None:
    assert _severity_for_label("local") == "warning"


def test_severity_for_managed_label() -> None:
    assert _severity_for_label("managed") == "error"


def test_severity_for_cli_overrides_label() -> None:
    assert _severity_for_label("cli_overrides") == "error"


# ---------------------------------------------------------------------------
# discover_config_paths
# ---------------------------------------------------------------------------


def test_discover_config_paths_returns_expected_labels(tmp_path: Path) -> None:
    """discover_config_paths should return all standard cascade layer paths."""
    paths = discover_config_paths(tmp_path)
    labels = [label for label, _ in paths]
    assert "user" in labels
    assert "project" in labels
    assert "project_alt" in labels
    assert "local" in labels
    assert "cli_overrides" in labels
    assert "managed" in labels
    # audit-157: sdd_project (.sdd/config.yaml) was removed because no loader reads it.
    assert "sdd_project" not in labels


def test_discover_config_paths_uses_workdir(tmp_path: Path) -> None:
    """Paths should be relative to the provided workdir."""
    paths = discover_config_paths(tmp_path)
    workdir_paths = [p for label, p in paths if label != "user"]
    for p in workdir_paths:
        assert str(p).startswith(str(tmp_path))


# ---------------------------------------------------------------------------
# ConfigWatcher.snapshot
# ---------------------------------------------------------------------------


def test_snapshot_captures_existing_files(tmp_path: Path) -> None:
    """snapshot() should record checksums for files that exist."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    project_entries = [e for e in watcher.baseline if e.label == "project"]
    assert len(project_entries) == 1
    assert project_entries[0].exists is True
    assert project_entries[0].checksum != ""


def test_snapshot_records_missing_files(tmp_path: Path) -> None:
    """snapshot() should record empty checksums for missing files."""
    watcher = ConfigWatcher.snapshot(tmp_path)
    # bernstein.yaml does not exist in tmp_path
    project_entries = [e for e in watcher.baseline if e.label == "project"]
    assert len(project_entries) == 1
    assert project_entries[0].exists is False
    assert project_entries[0].checksum == ""


def test_snapshot_sets_timestamp(tmp_path: Path) -> None:
    watcher = ConfigWatcher.snapshot(tmp_path)
    assert watcher.snapshot_at > 0


# ---------------------------------------------------------------------------
# ConfigWatcher.check — no drift
# ---------------------------------------------------------------------------


def test_check_no_drift_when_unchanged(tmp_path: Path) -> None:
    """check() should report no drift when no files have changed."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    report = watcher.check()

    assert report.drifted is False
    assert report.events == []


# ---------------------------------------------------------------------------
# ConfigWatcher.check — modified
# ---------------------------------------------------------------------------


def test_check_detects_modification(tmp_path: Path) -> None:
    """check() should detect when a config file is modified."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    seed.write_text("goal: changed\n", encoding="utf-8")
    report = watcher.check()

    assert report.drifted is True
    assert len(report.events) == 1
    event = report.events[0]
    assert event.kind == "modified"
    assert event.label == "project"
    assert event.severity == "warning"
    assert "modified" in event.summary()


# ---------------------------------------------------------------------------
# ConfigWatcher.check — created
# ---------------------------------------------------------------------------


def test_check_detects_creation(tmp_path: Path) -> None:
    """check() should detect when a previously missing config file is created."""
    watcher = ConfigWatcher.snapshot(tmp_path)

    # Create a config file that did not exist at snapshot time.
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: new\n", encoding="utf-8")

    report = watcher.check()
    assert report.drifted is True
    assert len(report.events) == 1
    event = report.events[0]
    assert event.kind == "created"
    assert event.label == "project"


# ---------------------------------------------------------------------------
# ConfigWatcher.check — deleted
# ---------------------------------------------------------------------------


def test_check_detects_deletion(tmp_path: Path) -> None:
    """check() should detect when a config file is deleted."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    seed.unlink()

    report = watcher.check()
    assert report.drifted is True
    assert len(report.events) == 1
    event = report.events[0]
    assert event.kind == "deleted"
    assert event.label == "project"


# ---------------------------------------------------------------------------
# Multiple simultaneous drifts
# ---------------------------------------------------------------------------


def test_check_detects_multiple_drifts(tmp_path: Path) -> None:
    """check() should report all drifted files, not just the first."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")
    local_dir = tmp_path / ".bernstein"
    local_dir.mkdir()
    local_cfg = local_dir / "config.yaml"
    local_cfg.write_text("cli: claude\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)

    # Modify both files.
    seed.write_text("goal: changed\n", encoding="utf-8")
    local_cfg.write_text("cli: codex\n", encoding="utf-8")

    report = watcher.check()
    assert report.drifted is True
    assert len(report.events) == 2
    labels = {e.label for e in report.events}
    assert "project" in labels
    assert "local" in labels


# ---------------------------------------------------------------------------
# Acknowledge
# ---------------------------------------------------------------------------


def test_acknowledge_suppresses_drift(tmp_path: Path) -> None:
    """After acknowledging drift, the same checksum should not trigger again."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    seed.write_text("goal: changed\n", encoding="utf-8")

    report = watcher.check()
    assert report.drifted is True

    # Acknowledge the drift.
    watcher.acknowledge_report(report)

    # Check again — should be clean now.
    report2 = watcher.check()
    assert report2.drifted is False


def test_acknowledge_does_not_suppress_further_change(tmp_path: Path) -> None:
    """If a file changes again after acknowledgement, drift should be re-reported."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: v1\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    seed.write_text("goal: v2\n", encoding="utf-8")

    report = watcher.check()
    watcher.acknowledge_report(report)

    # Another change.
    seed.write_text("goal: v3\n", encoding="utf-8")
    report2 = watcher.check()
    assert report2.drifted is True
    assert report2.events[0].kind == "modified"


# ---------------------------------------------------------------------------
# re_snapshot
# ---------------------------------------------------------------------------


def test_re_snapshot_resets_baseline(tmp_path: Path) -> None:
    """re_snapshot() should update the baseline so old drifts disappear."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    seed.write_text("goal: changed\n", encoding="utf-8")

    # Should detect drift.
    report = watcher.check()
    assert report.drifted is True

    # Re-snapshot to accept the new state.
    watcher.re_snapshot()

    # Now check should be clean.
    report2 = watcher.check()
    assert report2.drifted is False


def test_re_snapshot_clears_acknowledgements(tmp_path: Path) -> None:
    """re_snapshot() should clear the acknowledged set."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    seed.write_text("goal: changed\n", encoding="utf-8")
    report = watcher.check()
    watcher.acknowledge_report(report)

    watcher.re_snapshot()
    assert watcher.acknowledged == {}


# ---------------------------------------------------------------------------
# source_chain
# ---------------------------------------------------------------------------


def test_source_chain_returns_inspectable_list(tmp_path: Path) -> None:
    """source_chain() should return a list of dicts suitable for display."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    chain = watcher.source_chain()

    assert isinstance(chain, list)
    assert len(chain) > 0
    project_entry = next(e for e in chain if e["label"] == "project")
    assert project_entry["exists"] is True
    # Checksum should be truncated for display.
    checksum_str = project_entry["checksum"]
    assert isinstance(checksum_str, str)
    assert checksum_str.endswith("...")


def test_source_chain_missing_file_has_empty_checksum(tmp_path: Path) -> None:
    """Missing files should have empty checksum in source_chain."""
    watcher = ConfigWatcher.snapshot(tmp_path)
    chain = watcher.source_chain()
    project_entry = next(e for e in chain if e["label"] == "project")
    assert project_entry["exists"] is False
    assert project_entry["checksum"] == ""


# ---------------------------------------------------------------------------
# DriftReport.to_dict
# ---------------------------------------------------------------------------


def test_drift_report_to_dict_structure(tmp_path: Path) -> None:
    """DriftReport.to_dict() should produce a JSON-serializable structure."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    seed.write_text("goal: changed\n", encoding="utf-8")
    report = watcher.check()

    d = report.to_dict()
    assert d["drifted"] is True
    assert isinstance(d["events"], list)
    assert len(d["events"]) == 1
    event_dict = d["events"][0]
    assert "path" in event_dict
    assert "label" in event_dict
    assert "kind" in event_dict
    assert "severity" in event_dict
    assert "summary" in event_dict


def test_drift_report_no_drift_to_dict() -> None:
    """An empty DriftReport should serialize cleanly."""
    report = DriftReport(drifted=False, checked_at=1.0, snapshot_at=0.0)
    d = report.to_dict()
    assert d["drifted"] is False
    assert d["events"] == []


# ---------------------------------------------------------------------------
# Severity for managed/cli_overrides
# ---------------------------------------------------------------------------


def test_managed_file_drift_is_error(tmp_path: Path) -> None:
    """Drift in managed settings should produce severity='error'."""
    config_dir = tmp_path / ".sdd" / "config"
    config_dir.mkdir(parents=True)
    managed = config_dir / "managed_settings.json"
    managed.write_text('{"max_agents": 4}', encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    managed.write_text('{"max_agents": 8}', encoding="utf-8")

    report = watcher.check()
    assert report.drifted is True
    managed_events = [e for e in report.events if e.label == "managed"]
    assert len(managed_events) == 1
    assert managed_events[0].severity == "error"


def test_cli_overrides_drift_is_error(tmp_path: Path) -> None:
    """Drift in CLI overrides should produce severity='error'."""
    config_dir = tmp_path / ".sdd" / "config"
    config_dir.mkdir(parents=True)
    cli = config_dir / "cli_overrides.json"
    cli.write_text('{"model": "opus"}', encoding="utf-8")

    watcher = ConfigWatcher.snapshot(tmp_path)
    cli.write_text('{"model": "sonnet"}', encoding="utf-8")

    report = watcher.check()
    assert report.drifted is True
    cli_events = [e for e in report.events if e.label == "cli_overrides"]
    assert len(cli_events) == 1
    assert cli_events[0].severity == "error"


# ---------------------------------------------------------------------------
# DriftEvent.summary
# ---------------------------------------------------------------------------


def test_drift_event_summary_format() -> None:
    """DriftEvent.summary() should produce a parseable one-liner."""
    from bernstein.core.config_watcher import DriftEvent

    event = DriftEvent(
        path="/tmp/bernstein.yaml",
        label="project",
        kind="modified",
        old_checksum="abc123",
        new_checksum="def456",
        severity="warning",
        detected_at=1000.0,
    )
    s = event.summary()
    assert "warning" in s
    assert "project" in s
    assert "modified" in s
    assert "/tmp/bernstein.yaml" in s


# ---------------------------------------------------------------------------
# Source chain covers all cascade layers
# ---------------------------------------------------------------------------


def test_source_chain_covers_all_cascade_labels(tmp_path: Path) -> None:
    """source_chain() must include labels for every SettingsCascade file layer."""
    watcher = ConfigWatcher.snapshot(tmp_path)
    chain = watcher.source_chain()
    labels = {str(entry["label"]) for entry in chain}
    required = {"user", "project", "local", "managed", "cli_overrides"}
    assert required.issubset(labels), f"Missing labels: {required - labels}"


def test_source_chain_deterministic_order(tmp_path: Path) -> None:
    """Two snapshots of the same workdir must produce identical source chains."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: test\n", encoding="utf-8")

    w1 = ConfigWatcher.snapshot(tmp_path)
    w2 = ConfigWatcher.snapshot(tmp_path)

    c1 = [(e["label"], e["checksum"], e["exists"]) for e in w1.source_chain()]
    c2 = [(e["label"], e["checksum"], e["exists"]) for e in w2.source_chain()]
    assert c1 == c2


def test_source_chain_reflects_cascade_file_paths(tmp_path: Path) -> None:
    """ConfigWatcher must watch the same files that SettingsCascade loads."""
    from bernstein.core.config_watcher import discover_config_paths

    paths = discover_config_paths(tmp_path)
    watcher_labels = {label for label, _ in paths}

    # The watcher must cover at least: user, project, local, managed, cli_overrides
    assert "user" in watcher_labels
    assert "project" in watcher_labels
    assert "local" in watcher_labels
    assert "managed" in watcher_labels
    assert "cli_overrides" in watcher_labels
