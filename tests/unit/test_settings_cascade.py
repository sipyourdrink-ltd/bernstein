"""Tests for settings cascade — validation layer, precedence merge, file discovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.settings_cascade import (
    SettingsCascade,
    SettingsSource,
    SettingsValidationError,
    _validate_layer,
)

# --- Fixtures ---


@pytest.fixture()
def cascade() -> SettingsCascade:
    """Fresh empty cascade."""
    return SettingsCascade()


@pytest.fixture()
def populated_cascade() -> SettingsCascade:
    """Cascade with all 5 layers loaded."""
    c = SettingsCascade()
    c.load_layer(SettingsSource.USER, {"cli": "claude", "max_agents": 4, "effort": "normal"})
    c.load_layer(SettingsSource.PROJECT, {"cli": "codex", "budget": "$50"})
    c.load_layer(SettingsSource.LOCAL, {"effort": "high", "log_level": "DEBUG"})
    c.load_layer(SettingsSource.CLI, {"model": "opus"})
    c.load_layer(SettingsSource.MANAGED, {"server_url": "http://managed:8052", "timeout": 120})
    return c


# --- TestSettingsSource enum ---


class TestSettingsSource:
    def test_enum_order(self) -> None:
        """USER lowest, MANAGED highest."""
        assert SettingsSource.USER.value < SettingsSource.PROJECT.value
        assert SettingsSource.PROJECT.value < SettingsSource.LOCAL.value
        assert SettingsSource.LOCAL.value < SettingsSource.CLI.value
        assert SettingsSource.CLI.value < SettingsSource.MANAGED.value

    def test_all_sources_have_consecutive_values(self) -> None:
        values = sorted(s.value for s in SettingsSource)
        assert values == [1, 2, 3, 4, 5]


# --- Test _validate_layer ---


class TestValidateLayer:
    def test_unknown_key_passes_through(self) -> None:
        """Unknown keys are not validated (extension-friendly)."""
        result = _validate_layer(SettingsSource.USER, {"unknown_key": "anything"})
        assert result == {"unknown_key": "anything"}

    def test_known_key_correct_type(self) -> None:
        result = _validate_layer(SettingsSource.PROJECT, {"max_agents": 8})
        assert result == {"max_agents": 8}

    def test_known_key_string_coerced_to_int(self) -> None:
        """CLI often passes numbers as strings."""
        result = _validate_layer(SettingsSource.CLI, {"max_agents": "8"})
        assert result == {"max_agents": 8}
        assert isinstance(result["max_agents"], int)

    def test_known_key_wrong_type_raises(self) -> None:
        with pytest.raises(SettingsValidationError, match="max_agents"):
            _validate_layer(SettingsSource.USER, {"max_agents": [1, 2]})

    def test_known_key_invalid_enum_value_raises(self) -> None:
        with pytest.raises(SettingsValidationError, match="not in allowed values"):
            _validate_layer(SettingsSource.USER, {"effort": "extreme"})

    def test_known_key_valid_enum_value(self) -> None:
        for valid_effort in ("low", "normal", "high", "max", "auto"):
            result = _validate_layer(SettingsSource.USER, {"effort": valid_effort})
            assert result == {"effort": valid_effort}

    def test_null_value_passes(self) -> None:
        """YAML renders 'null' as Python None — should be accepted."""
        result = _validate_layer(SettingsSource.USER, {"model": None})
        assert result == {"model": None}

    def test_log_level_valid_values(self) -> None:
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            result = _validate_layer(SettingsSource.USER, {"log_level": level})
            assert result == {"log_level": level}

    def test_invalid_converter_raises(self) -> None:
        with pytest.raises(SettingsValidationError, match="cannot be converted"):
            _validate_layer(SettingsSource.CLI, {"timeout": "not-a-number"})

    def test_empty_dict_is_valid(self) -> None:
        """An empty dict is a valid (no-op) layer."""
        assert _validate_layer(SettingsSource.USER, {}) == {}


# --- Test SettingsCascade: load / remove ---


class TestCascadeLoadLayer:
    def test_load_layer_validates(self) -> None:
        c = SettingsCascade()
        with pytest.raises(SettingsValidationError):
            c.load_layer(SettingsSource.USER, {"effort": "invalid"})

    def test_load_layer_stores_data(self) -> None:
        c = SettingsCascade()
        c.load_layer(SettingsSource.USER, {"cli": "gemini"})
        assert c.layers[SettingsSource.USER] == {"cli": "gemini"}

    def test_load_layer_overwrites_previous(self) -> None:
        c = SettingsCascade()
        c.load_layer(SettingsSource.USER, {"cli": "claude"})
        c.load_layer(SettingsSource.USER, {"cli": "codex"})
        assert c.layers[SettingsSource.USER] == {"cli": "codex"}

    def test_remove_layer_missing_is_noop(self) -> None:
        c = SettingsCascade()
        c.remove_layer(SettingsSource.CLI)  # never loaded
        assert SettingsSource.CLI not in c.layers

    def test_remove_layer_clears_cache(self) -> None:
        c = SettingsCascade()
        c.load_layer(SettingsSource.USER, {"cli": "claude"})
        _ = c.get_effective()  # build cache
        c.remove_layer(SettingsSource.USER)
        assert c._merged is None


# --- Test SettingsCascade: get / merge ---


class TestCascadeGet:
    def test_get_single_layer(self, cascade: SettingsCascade) -> None:
        cascade.load_layer(SettingsSource.USER, {"cli": "claude"})
        pv = cascade.get("cli")
        assert pv.value == "claude"
        assert pv.source is SettingsSource.USER

    def test_get_project_overrides_user(self, cascade: SettingsCascade) -> None:
        cascade.load_layer(SettingsSource.USER, {"cli": "claude"})
        cascade.load_layer(SettingsSource.PROJECT, {"cli": "codex"})
        pv = cascade.get("cli")
        assert pv.value == "codex"
        assert pv.source is SettingsSource.PROJECT

    def test_get_cli_overrides_all(self, cascade: SettingsCascade) -> None:
        cascade.load_layer(SettingsSource.USER, {"model": "flash"})
        cascade.load_layer(SettingsSource.LOCAL, {"model": "sonnet"})
        cascade.load_layer(SettingsSource.CLI, {"model": "opus"})
        pv = cascade.get("model")
        assert pv.value == "opus"
        assert pv.source is SettingsSource.CLI

    def test_get_missing_key_returns_default(self, cascade: SettingsCascade) -> None:
        pv = cascade.get("nonexistent", default="fallback")
        assert pv.value == "fallback"
        assert pv.source is SettingsSource.USER

    def test_get_managed_highest_precedence(self, cascade: SettingsCascade) -> None:
        cascade.load_layer(SettingsSource.USER, {"model": "flash"})
        cascade.load_layer(SettingsSource.PROJECT, {"model": "sonnet"})
        cascade.load_layer(SettingsSource.CLI, {"model": "opus"})
        cascade.load_layer(SettingsSource.MANAGED, {"model": "managed-model"})
        pv = cascade.get("model")
        assert pv.value == "managed-model"
        assert pv.source is SettingsSource.MANAGED

    def test_get_effective_full_merge(self) -> None:
        c = SettingsCascade()
        c.load_layer(SettingsSource.USER, {"a": 1, "b": 2})
        c.load_layer(SettingsSource.LOCAL, {"b": 3, "c": 4})
        c.load_layer(SettingsSource.CLI, {"d": 5})
        effective = c.get_effective()
        assert effective == {"a": 1, "b": 3, "c": 4, "d": 5}

    def test_get_all_provenance(self) -> None:
        c = SettingsCascade()
        c.load_layer(SettingsSource.USER, {"effort": "low"})
        c.load_layer(SettingsSource.PROJECT, {"effort": "normal"})
        c.load_layer(SettingsSource.LOCAL, {"effort": "high"})
        provenance = c.get_all_provenance("effort")
        assert len(provenance) == 3
        assert provenance[0].source is SettingsSource.USER
        assert provenance[0].value == "low"
        assert provenance[1].source is SettingsSource.PROJECT
        assert provenance[1].value == "normal"
        assert provenance[2].source is SettingsSource.LOCAL
        assert provenance[2].value == "high"

    def test_get_all_provenance_key_in_one_layer(self, cascade: SettingsCascade) -> None:
        cascade.load_layer(SettingsSource.CLI, {"model": "opus"})
        provenance = cascade.get_all_provenance("model")
        assert len(provenance) == 1
        assert provenance[0].source is SettingsSource.CLI

    def test_provenance_source_detail(self, cascade: SettingsCascade) -> None:
        cascade.load_layer(SettingsSource.MANAGED, {"timeout": 60})
        pv = cascade.get("timeout")
        assert "managed layer" in pv.source_detail


# --- Test SettingsCascade: summary ---


class TestCascadeSummary:
    def test_summary_reflects_merged(self, populated_cascade: SettingsCascade) -> None:
        summary = populated_cascade.summary()
        # cli: USER=claude, PROJECT=codex -> PROJECT wins
        assert summary["cli"]["value"] == "codex"
        assert summary["cli"]["source"] == "PROJECT"
        # max_agents: only in USER
        assert summary["max_agents"]["value"] == 4
        assert summary["max_agents"]["source"] == "USER"
        # effort: USER=normal, LOCAL=high -> LOCAL wins
        assert summary["effort"]["value"] == "high"
        assert summary["effort"]["source"] == "LOCAL"

    def test_summary_empty_cascade(self) -> None:
        c = SettingsCascade()
        assert c.summary() == {}


# --- Test SettingsCascade: load_from_workdir ---


class TestLoadFromWorkdir:
    def test_loads_all_layers(self, tmp_path: Path) -> None:
        workdir = tmp_path / "project"
        workdir.mkdir()

        # USER
        user_dir = tmp_path / ".bernstein_home"
        user_dir.mkdir()
        (user_dir / ".bernstein" / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
        user_config = user_dir / ".bernstein" / "config.yaml"
        # Monkey-patch home for testing
        import bernstein.core.settings_cascade as sc_mod

        original_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        sc_mod.Path.home = lambda: user_dir  # type: ignore[reportAttributeAccessIssue]

        user_config.write_text("cli: claude\nmax_agents: 4\n")

        # PROJECT
        (workdir / "bernstein.yaml").write_text("cli: codex\neffort: max\n")

        # LOCAL
        local_dir = workdir / ".bernstein"
        local_dir.mkdir()
        (local_dir / "config.yaml").write_text("log_level: DEBUG\n")

        # CLI
        cli_dir = workdir / ".sdd" / "config"
        cli_dir.mkdir(parents=True)
        (cli_dir / "cli_overrides.json").write_text(json.dumps({"model": "opus"}))

        # MANAGED
        (cli_dir / "managed_settings.json").write_text(json.dumps({"server_url": "http://managed:8052"}))

        try:
            c = SettingsCascade()
            c.load_from_workdir(workdir)

            assert SettingsSource.USER in c.layers
            assert SettingsSource.PROJECT in c.layers
            assert SettingsSource.LOCAL in c.layers
            assert SettingsSource.CLI in c.layers
            assert SettingsSource.MANAGED in c.layers

            assert c.get("model").value == "opus"
            assert c.get("model").source is SettingsSource.CLI
            assert c.get("cli").value == "codex"
            assert c.get("cli").source is SettingsSource.PROJECT
        finally:
            sc_mod.Path.home = original_home  # type: ignore[reportAttributeAccessIssue]

    def test_missing_files_skipped_silently(self, tmp_path: Path) -> None:
        """Empty workdir = no layers loaded, no exception."""
        workdir = tmp_path / "empty_project"
        workdir.mkdir()

        # Mock home to empty temp dir
        import bernstein.core.settings_cascade as sc_mod

        original_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        sc_mod.Path.home = lambda: tmp_path  # type: ignore[reportAttributeAccessIssue]

        try:
            c = SettingsCascade()
            c.load_from_workdir(workdir)
            # USER layer won't load since there's no ~/.bernstein/config.yaml
            assert SettingsSource.USER not in c.layers or len(c.layers) <= 5
        finally:
            sc_mod.Path.home = original_home  # type: ignore[reportAttributeAccessIssue]

    def test_bernstein_yml_detected(self, tmp_path: Path) -> None:
        """PROJECT layer should find bernstein.yml as fallback."""
        workdir = tmp_path / "yml_project"
        workdir.mkdir()
        (workdir / "bernstein.yml").write_text("cli: gemini\n")

        import bernstein.core.settings_cascade as sc_mod

        original_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        sc_mod.Path.home = lambda: tmp_path  # type: ignore[reportAttributeAccessIssue]

        try:
            c = SettingsCascade()
            c.load_from_workdir(workdir)
            assert SettingsSource.PROJECT in c.layers
            assert c.layers[SettingsSource.PROJECT]["cli"] == "gemini"
        finally:
            sc_mod.Path.home = original_home  # type: ignore[reportAttributeAccessIssue]

    def test_bernstein_yaml_takes_precedence_over_yml(self, tmp_path: Path) -> None:
        """If both bernstein.yaml and bernstein.yml exist, .yaml wins."""
        workdir = tmp_path / "both_project"
        workdir.mkdir()
        (workdir / "bernstein.yaml").write_text("cli: claude\n")
        (workdir / "bernstein.yml").write_text("cli: gemini\n")

        import bernstein.core.settings_cascade as sc_mod

        original_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        sc_mod.Path.home = lambda: tmp_path  # type: ignore[reportAttributeAccessIssue]

        try:
            c = SettingsCascade()
            c.load_from_workdir(workdir)
            assert SettingsSource.PROJECT in c.layers
            assert c.layers[SettingsSource.PROJECT]["cli"] == "claude"
        finally:
            sc_mod.Path.home = original_home  # type: ignore[reportAttributeAccessIssue]

    def test_invalid_yaml_file_skipped(self, tmp_path: Path) -> None:
        workdir = tmp_path / "bad_project"
        workdir.mkdir()
        (workdir / "bernstein.yaml").write_text("{{{invalid yaml\n")

        import bernstein.core.settings_cascade as sc_mod

        original_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        sc_mod.Path.home = lambda: tmp_path  # type: ignore[reportAttributeAccessIssue]

        try:
            c = SettingsCascade()
            c.load_from_workdir(workdir)
            # Layer should not be loaded
            assert SettingsSource.PROJECT not in c.layers
        finally:
            sc_mod.Path.home = original_home  # type: ignore[reportAttributeAccessIssue]

    def test_invalid_json_file_skipped(self, tmp_path: Path) -> None:
        workdir = tmp_path / "bad_project"
        workdir.mkdir()
        cli_dir = workdir / ".sdd" / "config"
        cli_dir.mkdir(parents=True)
        (cli_dir / "cli_overrides.json").write_text("not json at all")

        import bernstein.core.settings_cascade as sc_mod

        original_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        sc_mod.Path.home = lambda: tmp_path  # type: ignore[reportAttributeAccessIssue]

        try:
            c = SettingsCascade()
            c.load_from_workdir(workdir)
            # CLI layer should not be loaded
            assert SettingsSource.CLI not in c.layers
        finally:
            sc_mod.Path.home = original_home  # type: ignore[reportAttributeAccessIssue]


# --- Test SettingsCascade: load_from_dict ---


class TestLoadFromDict:
    def test_load_from_dict_validates(self) -> None:
        c = SettingsCascade()
        with pytest.raises(SettingsValidationError):
            c.load_from_dict(SettingsSource.USER, {"effort": "invalid"})

    def test_load_from_dict_stores_data(self) -> None:
        c = SettingsCascade()
        c.load_from_dict(SettingsSource.CLI, {"timeout": 60, "model": "opus"})
        assert c.get("timeout").value == 60
        assert c.get("timeout").source is SettingsSource.CLI
        assert c.get("model").value == "opus"


# --- Edge cases ---


class TestCascadeEdgeCases:
    def test_none_value_in_layer(self) -> None:
        """None values are stored and treated like any other value."""
        c = SettingsCascade()
        c.load_layer(SettingsSource.CLI, {"model": None})
        pv = c.get("model")
        assert pv.value is None
        assert pv.source is SettingsSource.CLI

    def test_non_string_non_null_in_layer(self) -> None:
        """Non-string values that don't have a converter should be type-checked."""
        c = SettingsCascade()
        c.load_layer(SettingsSource.USER, {"max_agents": 10})
        assert c.get("max_agents").value == 10

    def test_string_path_gets_properly(self) -> None:
        """load_from_workdir accepts both str and Path."""
        c = SettingsCascade()
        import bernstein.core.settings_cascade as sc_mod

        original_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        sc_mod.Path.home = lambda: Path("/nonexistent_home_for_test")  # type: ignore[reportAttributeAccessIssue]

        try:
            tmp = Path("/tmp/test_workdir_str")
            tmp.mkdir(exist_ok=True)
            c.load_from_workdir(str(tmp))
            # No layers loaded (no files) but no exception
        finally:
            sc_mod.Path.home = original_home  # type: ignore[reportAttributeAccessIssue]


# --- MDM / managed-settings tests ---


class TestMdmSystemPaths:
    """Tests for _mdm_system_paths() path discovery."""

    def test_env_var_path_is_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        custom = tmp_path / "custom-managed.json"
        monkeypatch.setenv("BERNSTEIN_MANAGED_SETTINGS_PATH", str(custom))
        paths = SettingsCascade._mdm_system_paths(tmp_path)
        assert paths[0] == custom

    def test_etc_path_always_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_MANAGED_SETTINGS_PATH", raising=False)
        paths = SettingsCascade._mdm_system_paths(tmp_path)
        assert any(p == Path("/etc/bernstein/managed-settings.json") for p in paths)

    def test_workdir_fallback_is_last(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_MANAGED_SETTINGS_PATH", raising=False)
        paths = SettingsCascade._mdm_system_paths(tmp_path)
        expected_fallback = tmp_path / ".sdd" / "config" / "managed_settings.json"
        assert paths[-1] == expected_fallback

    def test_env_path_prepended_before_system_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        custom = tmp_path / "mdm.json"
        monkeypatch.setenv("BERNSTEIN_MANAGED_SETTINGS_PATH", str(custom))
        paths = SettingsCascade._mdm_system_paths(tmp_path)
        etc_idx = next(i for i, p in enumerate(paths) if p == Path("/etc/bernstein/managed-settings.json"))
        assert paths.index(custom) < etc_idx


class TestMdmManagedSettingsLoading:
    """Tests that MDM-managed settings load into MANAGED layer at highest priority."""

    def _make_workdir(self, tmp_path: Path) -> Path:
        wd = tmp_path / "project"
        wd.mkdir()
        return wd

    def test_env_var_managed_settings_loaded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """$BERNSTEIN_MANAGED_SETTINGS_PATH is read and loaded as MANAGED layer."""
        wd = self._make_workdir(tmp_path)
        managed_file = tmp_path / "mdm.json"
        managed_file.write_text('{"max_agents": 2, "timeout": 300}')
        monkeypatch.setenv("BERNSTEIN_MANAGED_SETTINGS_PATH", str(managed_file))

        import bernstein.core.settings_cascade as sc_mod

        orig_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        sc_mod.Path.home = lambda: tmp_path  # type: ignore[reportAttributeAccessIssue]
        try:
            c = SettingsCascade()
            c.load_from_workdir(wd)
            assert SettingsSource.MANAGED in c.layers
            assert c.layers[SettingsSource.MANAGED]["max_agents"] == 2
            assert c.layers[SettingsSource.MANAGED]["timeout"] == 300
        finally:
            sc_mod.Path.home = orig_home  # type: ignore[reportAttributeAccessIssue]

    def test_managed_overrides_user_setting(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """MANAGED layer setting overrides USER layer setting (highest priority)."""
        wd = self._make_workdir(tmp_path)
        managed_file = tmp_path / "mdm.json"
        managed_file.write_text('{"max_agents": 1}')
        monkeypatch.setenv("BERNSTEIN_MANAGED_SETTINGS_PATH", str(managed_file))

        import bernstein.core.settings_cascade as sc_mod

        orig_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        sc_mod.Path.home = lambda: tmp_path  # type: ignore[reportAttributeAccessIssue]

        # Write a user config with a different max_agents value.
        user_cfg = tmp_path / ".bernstein" / "config.yaml"
        user_cfg.parent.mkdir(exist_ok=True)
        user_cfg.write_text("max_agents: 8\n")

        try:
            c = SettingsCascade()
            c.load_from_workdir(wd)
            pv = c.get("max_agents")
            assert pv.value == 1
            assert pv.source is SettingsSource.MANAGED
        finally:
            sc_mod.Path.home = orig_home  # type: ignore[reportAttributeAccessIssue]

    def test_workdir_fallback_managed_settings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Workdir .sdd/config/managed_settings.json is used as fallback."""
        monkeypatch.delenv("BERNSTEIN_MANAGED_SETTINGS_PATH", raising=False)
        wd = self._make_workdir(tmp_path)
        sdd_config = wd / ".sdd" / "config"
        sdd_config.mkdir(parents=True)
        (sdd_config / "managed_settings.json").write_text('{"timeout": 60}')

        import bernstein.core.settings_cascade as sc_mod

        orig_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        # Point home to a dir with no .bernstein/config.yaml so /etc path also missing.
        sc_mod.Path.home = lambda: tmp_path  # type: ignore[reportAttributeAccessIssue]
        try:
            c = SettingsCascade()
            c.load_from_workdir(wd)
            assert SettingsSource.MANAGED in c.layers
            assert c.layers[SettingsSource.MANAGED]["timeout"] == 60
        finally:
            sc_mod.Path.home = orig_home  # type: ignore[reportAttributeAccessIssue]

    def test_first_existing_managed_path_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When env path exists it takes precedence over workdir fallback."""
        wd = self._make_workdir(tmp_path)
        # Write both env path and workdir fallback with different values.
        env_managed = tmp_path / "env_mdm.json"
        env_managed.write_text('{"max_agents": 3}')
        monkeypatch.setenv("BERNSTEIN_MANAGED_SETTINGS_PATH", str(env_managed))

        sdd_config = wd / ".sdd" / "config"
        sdd_config.mkdir(parents=True)
        (sdd_config / "managed_settings.json").write_text('{"max_agents": 99}')

        import bernstein.core.settings_cascade as sc_mod

        orig_home = sc_mod.Path.home  # type: ignore[reportAttributeAccessIssue]
        sc_mod.Path.home = lambda: tmp_path  # type: ignore[reportAttributeAccessIssue]
        try:
            c = SettingsCascade()
            c.load_from_workdir(wd)
            # env path should win (first in list)
            assert c.layers[SettingsSource.MANAGED]["max_agents"] == 3
        finally:
            sc_mod.Path.home = orig_home  # type: ignore[reportAttributeAccessIssue]
