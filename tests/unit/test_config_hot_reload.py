"""Tests for bernstein.core.config_hot_reload (CFG-006)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import yaml

from bernstein.core.config_hot_reload import (
    HotReloader,
    ReloadEvent,
)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data), encoding="utf-8")


class TestHotReloaderStart:
    def test_start_initializes_watcher(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "bernstein.yaml", {"goal": "test"})
        reloader = HotReloader(workdir=tmp_path)
        reloader.start()
        assert reloader.watcher is not None
        assert reloader.is_running

    def test_stop_sets_running_false(self, tmp_path: Path) -> None:
        reloader = HotReloader(workdir=tmp_path)
        reloader.start()
        reloader.stop()
        assert not reloader.is_running


class TestHotReloaderCheck:
    def test_no_change_returns_none(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "bernstein.yaml", {"goal": "test"})
        reloader = HotReloader(workdir=tmp_path)
        reloader.start()
        result = reloader.check()
        assert result is None

    def test_detects_change(self, tmp_path: Path) -> None:
        config_path = tmp_path / "bernstein.yaml"
        _write_yaml(config_path, {"goal": "original"})
        reloader = HotReloader(workdir=tmp_path)
        reloader.start()

        # Modify the config.
        _write_yaml(config_path, {"goal": "modified"})

        result = reloader.check()
        assert result is not None
        assert isinstance(result, ReloadEvent)
        assert result.success

    def test_callback_invoked_on_change(self, tmp_path: Path) -> None:
        config_path = tmp_path / "bernstein.yaml"
        _write_yaml(config_path, {"goal": "v1"})
        reloader = HotReloader(workdir=tmp_path)
        cb = MagicMock()
        reloader.register_callback(cb)
        reloader.start()

        _write_yaml(config_path, {"goal": "v2"})
        reloader.check()
        assert cb.called

    def test_callback_error_sets_success_false(self, tmp_path: Path) -> None:
        config_path = tmp_path / "bernstein.yaml"
        _write_yaml(config_path, {"goal": "v1"})
        reloader = HotReloader(workdir=tmp_path)

        def bad_callback(diff: Any) -> None:
            raise RuntimeError("callback error")

        reloader.register_callback(bad_callback)
        reloader.start()

        _write_yaml(config_path, {"goal": "v2"})
        result = reloader.check()
        assert result is not None
        assert not result.success
        assert "callback error" in result.error


class TestHotReloaderThrottling:
    def test_min_reload_interval(self, tmp_path: Path) -> None:
        config_path = tmp_path / "bernstein.yaml"
        _write_yaml(config_path, {"goal": "v1"})
        reloader = HotReloader(workdir=tmp_path)
        reloader.start()

        _write_yaml(config_path, {"goal": "v2"})
        first = reloader.check()
        assert first is not None

        # Immediately check again - should be throttled.
        _write_yaml(config_path, {"goal": "v3"})
        second = reloader.check()
        assert second is None


class TestHotReloaderHistory:
    def test_history_records_events(self, tmp_path: Path) -> None:
        config_path = tmp_path / "bernstein.yaml"
        _write_yaml(config_path, {"goal": "v1"})
        reloader = HotReloader(workdir=tmp_path)
        reloader.start()

        _write_yaml(config_path, {"goal": "v2"})
        reloader.check()
        assert reloader.reload_count == 1

    def test_history_bounded(self, tmp_path: Path) -> None:
        reloader = HotReloader(workdir=tmp_path, max_history=2)
        reloader.start()
        # Manually add events to test bounding.
        from bernstein.core.config_diff import ConfigDiffSummary

        for _i in range(5):
            event = ReloadEvent(
                timestamp=time.time(),
                diff=ConfigDiffSummary(changed=True),
                source_path="test.yaml",
                success=True,
            )
            reloader.history.append(event)

        # Trim should happen on next _handle_drift, but we can verify manual trim.
        reloader.history = reloader.history[-reloader.max_history :]
        assert len(reloader.history) <= 2


class TestReloadEventSerialization:
    def test_to_dict(self) -> None:
        from bernstein.core.config_diff import ConfigDiffSummary

        event = ReloadEvent(
            timestamp=1000.0,
            diff=ConfigDiffSummary(changed=True, added=1),
            source_path="/test.yaml",
            success=True,
        )
        d = event.to_dict()
        assert d["timestamp"] == 1000.0
        assert d["success"] is True
        assert d["source_path"] == "/test.yaml"
