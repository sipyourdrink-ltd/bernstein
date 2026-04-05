"""Tests for progressive disclosure view mode system."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.view_mode import ViewConfig, ViewMode, get_view_config, load_view_mode, save_view_mode


# ---------------------------------------------------------------------------
# ViewConfig flag correctness per mode
# ---------------------------------------------------------------------------


class TestGetViewConfig:
    """Ensure each mode produces the expected boolean flags."""

    def test_novice_hides_everything(self) -> None:
        vc = get_view_config(ViewMode.NOVICE)
        assert vc.mode is ViewMode.NOVICE
        assert vc.show_tokens is False
        assert vc.show_cost_per_task is False
        assert vc.show_model_details is False
        assert vc.show_agent_ids is False
        assert vc.show_quality_gates is False
        assert vc.show_error_traces is False

    def test_standard_shows_cost_and_quality(self) -> None:
        vc = get_view_config(ViewMode.STANDARD)
        assert vc.mode is ViewMode.STANDARD
        assert vc.show_tokens is False
        assert vc.show_cost_per_task is True
        assert vc.show_model_details is False
        assert vc.show_agent_ids is False
        assert vc.show_quality_gates is True
        assert vc.show_error_traces is False

    def test_expert_shows_all(self) -> None:
        vc = get_view_config(ViewMode.EXPERT)
        assert vc.mode is ViewMode.EXPERT
        assert vc.show_tokens is True
        assert vc.show_cost_per_task is True
        assert vc.show_model_details is True
        assert vc.show_agent_ids is True
        assert vc.show_quality_gates is True
        assert vc.show_error_traces is True


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


class TestPersistence:
    """Load/save round-trip through .sdd/config.yaml."""

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        save_view_mode(tmp_path, ViewMode.EXPERT)
        assert load_view_mode(tmp_path) is ViewMode.EXPERT

    def test_roundtrip_novice(self, tmp_path: Path) -> None:
        save_view_mode(tmp_path, ViewMode.NOVICE)
        assert load_view_mode(tmp_path) is ViewMode.NOVICE

    def test_roundtrip_standard(self, tmp_path: Path) -> None:
        save_view_mode(tmp_path, ViewMode.STANDARD)
        assert load_view_mode(tmp_path) is ViewMode.STANDARD

    def test_overwrite_preserves_other_keys(self, tmp_path: Path) -> None:
        """Saving view_mode must not clobber unrelated keys."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        config = sdd / "config.yaml"
        config.write_text("budget: 50\nmax_agents: 4\n", encoding="utf-8")

        save_view_mode(tmp_path, ViewMode.EXPERT)

        import yaml

        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data["view_mode"] == "expert"
        assert data["budget"] == 50
        assert data["max_agents"] == 4


# ---------------------------------------------------------------------------
# Default behaviour
# ---------------------------------------------------------------------------


class TestDefaults:
    """Default mode when config is absent or malformed."""

    def test_default_is_standard_when_no_sdd(self, tmp_path: Path) -> None:
        assert load_view_mode(tmp_path) is ViewMode.STANDARD

    def test_default_is_standard_when_key_missing(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        (sdd / "config.yaml").write_text("budget: 10\n", encoding="utf-8")
        assert load_view_mode(tmp_path) is ViewMode.STANDARD

    def test_default_on_invalid_value(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        (sdd / "config.yaml").write_text("view_mode: banana\n", encoding="utf-8")
        assert load_view_mode(tmp_path) is ViewMode.STANDARD

    def test_default_on_non_string_value(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        (sdd / "config.yaml").write_text("view_mode: 42\n", encoding="utf-8")
        assert load_view_mode(tmp_path) is ViewMode.STANDARD

    def test_default_on_empty_file(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        (sdd / "config.yaml").write_text("", encoding="utf-8")
        assert load_view_mode(tmp_path) is ViewMode.STANDARD


# ---------------------------------------------------------------------------
# ViewConfig is frozen
# ---------------------------------------------------------------------------


class TestViewConfigFrozen:
    """ViewConfig should be immutable (frozen dataclass)."""

    def test_frozen(self) -> None:
        vc = get_view_config(ViewMode.NOVICE)
        with pytest.raises(AttributeError):
            vc.show_tokens = True  # type: ignore[misc]
