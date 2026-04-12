"""Tests for settings_snapshot — settings capture and serialization."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import pytest

from bernstein.cli.settings_snapshot import (
    SettingsSnapshot,
    SettingValue,
    _read_config_file,
    capture_settings_snapshot,
    format_snapshot,
    save_settings_snapshot,
)


@pytest.fixture()
def settings_snapshot() -> SettingsSnapshot:
    """Create a sample settings snapshot."""
    from datetime import datetime

    return SettingsSnapshot(
        captured_at=datetime(2025, 1, 1, tzinfo=UTC),
        settings={
            "model": SettingValue(key="model", value="sonnet", source="env", source_detail="BERNSTEIN_MODEL"),
            "effort": SettingValue(key="effort", value="high", source="config", source_detail=".bernstein/config.yaml"),
            "timeout": SettingValue(key="timeout", value=300, source="default"),
        },
        env_vars={"BERNSTEIN_MODEL": "sonnet"},
        config_paths=[".bernstein/config.yaml"],
    )


@pytest.fixture()
def config_yaml(tmp_path: Path) -> Path:
    """Create a YAML config file."""
    f = tmp_path / "config.yaml"
    f.write_text("model: sonnet\neffort: high\ntimeout: 600\n", encoding="utf-8")
    return f


@pytest.fixture()
def config_json(tmp_path: Path) -> Path:
    """Create a JSON config file."""
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"model": "opus", "effort": "max"}), encoding="utf-8")
    return f


# --- TestSettingValue ---


class TestSettingValue:
    def test_defaults(self) -> None:
        sv = SettingValue(key="model", value="sonnet", source="env")
        assert sv.key == "model"
        assert sv.value == "sonnet"
        assert sv.source == "env"
        assert sv.source_detail == ""


# --- TestSettingsSnapshot ---


class TestSettingsSnapshot:
    def test_to_dict(self, settings_snapshot: SettingsSnapshot) -> None:
        d = settings_snapshot.to_dict()
        assert "captured_at" in d
        assert "settings" in d
        assert "env_vars" in d
        assert "config_paths" in d

    def test_get_existing(self, settings_snapshot: SettingsSnapshot) -> None:
        assert settings_snapshot.get("model") == "sonnet"

    def test_get_missing(self, settings_snapshot: SettingsSnapshot) -> None:
        assert settings_snapshot.get("missing", "default") == "default"

    def test_get_missing_no_default(self, settings_snapshot: SettingsSnapshot) -> None:
        assert settings_snapshot.get("missing") is None


# --- TestReadConfigFile ---


class TestReadConfigFile:
    def test_reads_json(self, config_json: Path) -> None:
        data = _read_config_file(config_json)
        assert data["model"] == "opus"
        assert data["effort"] == "max"

    def test_missing_file(self, tmp_path: Path) -> None:
        data = _read_config_file(tmp_path / "missing.json")
        assert data == {}

    def test_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("not json {{{", encoding="utf-8")
        data = _read_config_file(f)
        assert data == {}


# --- TestCaptureSettingsSnapshot ---


class TestCaptureSettingsSnapshot:
    def test_captures_defaults(self) -> None:
        snapshot = capture_settings_snapshot()
        assert snapshot.get("model") == "auto"
        assert snapshot.get("effort") == "normal"
        assert snapshot.get("timeout") == 300

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_MODEL", "claude-3")
        snapshot = capture_settings_snapshot()
        model_setting = snapshot.settings.get("model")
        assert model_setting is not None
        assert model_setting.value == "claude-3"
        assert model_setting.source == "env"

    def test_extra_overrides(self) -> None:
        snapshot = capture_settings_snapshot(extra_env={"model": "custom"})
        model_setting = snapshot.settings.get("model")
        assert model_setting is not None
        assert model_setting.value == "custom"
        assert model_setting.source == "cli"

    def test_env_vars_collected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_MODEL", "sonnet")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-123")
        snapshot = capture_settings_snapshot()
        assert "BERNSTEIN_MODEL" in snapshot.env_vars
        assert "ANTHROPIC_API_KEY" in snapshot.env_vars

    def test_captured_at_set(self) -> None:
        snapshot = capture_settings_snapshot()
        assert snapshot.captured_at is not None


# --- TestSaveSettingsSnapshot ---


class TestSaveSettingsSnapshot:
    def test_saves_file(self, settings_snapshot: SettingsSnapshot, tmp_path: Path) -> None:
        traces_dir = tmp_path / "traces"
        path = save_settings_snapshot(settings_snapshot, traces_dir)
        assert path.exists()
        assert path.suffix == ".json"

        data = json.loads(path.read_text())
        assert "settings" in data
        assert data["settings"]["model"]["value"] == "sonnet"

    def test_custom_filename(self, settings_snapshot: SettingsSnapshot, tmp_path: Path) -> None:
        traces_dir = tmp_path / "traces"
        path = save_settings_snapshot(settings_snapshot, traces_dir, filename="test.json")
        assert path.name == "test.json"


# --- TestFormatSnapshot ---


class TestFormatSnapshot:
    def test_format(self, settings_snapshot: SettingsSnapshot) -> None:
        output = format_snapshot(settings_snapshot)
        assert "Settings Snapshot" in output
        assert "model" in output
        assert "sonnet" in output

    def test_masks_sensitive_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-1234567890abcdef")
        snapshot = capture_settings_snapshot()
        output = format_snapshot(snapshot)
        assert "ANTHROPIC_API_KEY" in output
        # Should be masked
        assert "sk-12345..." in output
        assert "sk-1234567890abcdef" not in output
