"""Tests for bernstein.cli.tip_integration -- cli-019."""

from __future__ import annotations

import time
from pathlib import Path

from bernstein.cli.tip_integration import (
    COMMAND_TIP_MAP,
    CONTEXTUAL_TRIGGERS,
    TIP_COOLDOWN_SECONDS,
    TipTrigger,
    get_tip_for_command,
    mark_tip_shown,
    should_show_tip,
)
from bernstein.contextual_tips import TipsCatalog


class TestCommandTipMap:
    """COMMAND_TIP_MAP contains expected commands."""

    def test_has_run(self) -> None:
        assert "run" in COMMAND_TIP_MAP

    def test_has_stop(self) -> None:
        assert "stop" in COMMAND_TIP_MAP

    def test_has_status(self) -> None:
        assert "status" in COMMAND_TIP_MAP

    def test_has_cost(self) -> None:
        assert "cost" in COMMAND_TIP_MAP

    def test_has_agents(self) -> None:
        assert "agents" in COMMAND_TIP_MAP

    def test_has_doctor(self) -> None:
        assert "doctor" in COMMAND_TIP_MAP

    def test_run_categories(self) -> None:
        assert COMMAND_TIP_MAP["run"] == ["productivity", "general"]

    def test_stop_categories(self) -> None:
        assert COMMAND_TIP_MAP["stop"] == ["troubleshooting"]


class TestTipTrigger:
    """TipTrigger creation and immutability."""

    def test_create(self) -> None:
        t = TipTrigger(command="run", condition="first_run", tip_text="hello")
        assert t.command == "run"
        assert t.condition == "first_run"
        assert t.tip_text == "hello"

    def test_frozen(self) -> None:
        t = TipTrigger(command="run", condition="first_run", tip_text="hello")
        try:
            t.command = "stop"  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass  # expected for frozen dataclass

    def test_contextual_triggers_populated(self) -> None:
        assert len(CONTEXTUAL_TRIGGERS) >= 4
        commands = {t.command for t in CONTEXTUAL_TRIGGERS}
        assert "run" in commands
        assert "stop" in commands
        assert "cost" in commands


class TestGetTipForCommand:
    """get_tip_for_command returns tips correctly."""

    def test_known_command_returns_tip(self) -> None:
        catalog = TipsCatalog()  # uses default built-in tips
        tip = get_tip_for_command("run", catalog=catalog)
        assert tip is not None
        assert isinstance(tip, str)
        assert len(tip) > 0

    def test_unknown_command_returns_none(self) -> None:
        tip = get_tip_for_command("nonexistent_command_xyz")
        assert tip is None

    def test_contextual_trigger_always(self) -> None:
        """'stop' has an 'always' trigger -- it should fire without context."""
        tip = get_tip_for_command("stop")
        assert tip is not None
        assert "bernstein status" in tip

    def test_contextual_trigger_with_matching_condition(self) -> None:
        tip = get_tip_for_command("run", context={"headless_missing": True})
        assert tip is not None
        assert "--headless" in tip

    def test_contextual_trigger_condition_false_falls_through(self) -> None:
        """When condition is False, skip trigger and fall back to catalog."""
        catalog = TipsCatalog()
        tip = get_tip_for_command(
            "run",
            context={"headless_missing": False},
            catalog=catalog,
        )
        # Should get a catalog tip, not the trigger tip
        assert tip is not None
        # The catalog tip should NOT be the headless trigger
        assert tip != "Tip: Use --headless for CI runs"

    def test_cost_over_budget_trigger(self) -> None:
        tip = get_tip_for_command("cost", context={"over_budget": True})
        assert tip is not None
        assert "cost --export" in tip

    def test_no_budget_trigger(self) -> None:
        tip = get_tip_for_command("run", context={"no_budget": True})
        assert tip is not None
        assert "budget" in tip.lower()


class TestShouldShowTip:
    """should_show_tip respects file-based cooldown."""

    def test_no_file_returns_true(self, tmp_path: Path) -> None:
        marker = tmp_path / "last_shown"
        assert should_show_tip(cooldown_path=marker) is True

    def test_recent_file_returns_false(self, tmp_path: Path) -> None:
        marker = tmp_path / "last_shown"
        marker.touch()
        now = marker.stat().st_mtime + 10  # only 10s after
        assert should_show_tip(cooldown_path=marker, now=now) is False

    def test_old_file_returns_true(self, tmp_path: Path) -> None:
        marker = tmp_path / "last_shown"
        marker.touch()
        now = marker.stat().st_mtime + TIP_COOLDOWN_SECONDS + 1
        assert should_show_tip(cooldown_path=marker, now=now) is True

    def test_custom_cooldown(self, tmp_path: Path) -> None:
        marker = tmp_path / "last_shown"
        marker.touch()
        now = marker.stat().st_mtime + 61  # 61s later
        assert should_show_tip(
            cooldown_path=marker, now=now, cooldown_seconds=60
        ) is True

    def test_mark_tip_shown_creates_file(self, tmp_path: Path) -> None:
        marker = tmp_path / "subdir" / "last_shown"
        assert not marker.exists()
        mark_tip_shown(cooldown_path=marker)
        assert marker.exists()

    def test_mark_then_show_respects_cooldown(self, tmp_path: Path) -> None:
        marker = tmp_path / "last_shown"
        mark_tip_shown(cooldown_path=marker)
        now = time.time()
        assert should_show_tip(cooldown_path=marker, now=now) is False
