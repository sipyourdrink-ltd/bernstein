"""Tests for graduated memory guard (ORCH-011)."""

from __future__ import annotations

from bernstein.core.graduated_memory_guard import (
    GraduatedMemoryGuard,
    MemoryAction,
    MemoryGuardResponse,
    MemoryPressureLevel,
    MemoryStatus,
    _level_severity,
)


def _status(used_pct: float) -> MemoryStatus:
    """Create a MemoryStatus with the given utilization percentage."""
    total = 16 * 1024 * 1024 * 1024  # 16 GB
    available = int(total * (1 - used_pct / 100))
    return MemoryStatus(total_bytes=total, available_bytes=available, used_percent=used_pct)


class TestMemoryPressureLevels:
    def test_normal_below_80(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(50.0))
        assert resp.level == MemoryPressureLevel.NORMAL
        assert MemoryAction.NONE in resp.actions

    def test_warning_at_80(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(80.0))
        assert resp.level == MemoryPressureLevel.WARNING
        assert MemoryAction.PAUSE_SPAWNS in resp.actions
        assert MemoryAction.TRIGGER_GC in resp.actions

    def test_warning_at_85(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(85.0))
        assert resp.level == MemoryPressureLevel.WARNING

    def test_critical_at_90(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(90.0))
        assert resp.level == MemoryPressureLevel.CRITICAL
        assert MemoryAction.DRAIN_LOW_PRIORITY in resp.actions

    def test_emergency_at_95(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(95.0))
        assert resp.level == MemoryPressureLevel.EMERGENCY
        assert MemoryAction.EMERGENCY_SHUTDOWN in resp.actions

    def test_emergency_at_99(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(99.0))
        assert resp.level == MemoryPressureLevel.EMERGENCY


class TestCustomThresholds:
    def test_lower_warning_threshold(self) -> None:
        guard = GraduatedMemoryGuard(warning_pct=60.0)
        resp = guard.evaluate(_status(65.0))
        assert resp.level == MemoryPressureLevel.WARNING

    def test_higher_emergency_threshold(self) -> None:
        guard = GraduatedMemoryGuard(emergency_pct=99.0)
        resp = guard.evaluate(_status(96.0))
        assert resp.level == MemoryPressureLevel.CRITICAL


class TestCooldown:
    def test_cooldown_prevents_rapid_de_escalation(self) -> None:
        guard = GraduatedMemoryGuard(cooldown_s=60.0)
        # First call at critical
        resp1 = guard.evaluate(_status(92.0))
        assert resp1.level == MemoryPressureLevel.CRITICAL

        # Immediate call at normal — should stay at critical due to cooldown
        resp2 = guard.evaluate(_status(50.0))
        assert resp2.level == MemoryPressureLevel.CRITICAL


class TestGCTrigger:
    def test_gc_triggered_at_warning(self) -> None:
        guard = GraduatedMemoryGuard()
        assert guard._gc_triggered is False
        guard.evaluate(_status(82.0))
        assert guard._gc_triggered is True

    def test_gc_reset_on_recovery(self) -> None:
        guard = GraduatedMemoryGuard(cooldown_s=0.0)
        guard.evaluate(_status(85.0))
        assert guard._gc_triggered is True
        guard.evaluate(_status(50.0))
        assert guard._gc_triggered is False


class TestResponseMessage:
    def test_normal_message(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(40.0))
        assert "normal" in resp.message.lower()

    def test_warning_message(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(82.0))
        assert "WARNING" in resp.message

    def test_emergency_message(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(96.0))
        assert "EMERGENCY" in resp.message


class TestLevelSeverity:
    def test_ordering(self) -> None:
        assert _level_severity(MemoryPressureLevel.NORMAL) < _level_severity(MemoryPressureLevel.WARNING)
        assert _level_severity(MemoryPressureLevel.WARNING) < _level_severity(MemoryPressureLevel.CRITICAL)
        assert _level_severity(MemoryPressureLevel.CRITICAL) < _level_severity(MemoryPressureLevel.EMERGENCY)


class TestActions:
    def test_emergency_has_all_actions(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(97.0))
        assert len(resp.actions) == 4
        assert MemoryAction.PAUSE_SPAWNS in resp.actions
        assert MemoryAction.TRIGGER_GC in resp.actions
        assert MemoryAction.DRAIN_LOW_PRIORITY in resp.actions
        assert MemoryAction.EMERGENCY_SHUTDOWN in resp.actions

    def test_normal_only_none(self) -> None:
        guard = GraduatedMemoryGuard()
        resp = guard.evaluate(_status(30.0))
        assert resp.actions == [MemoryAction.NONE]
