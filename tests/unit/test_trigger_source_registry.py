"""Regression coverage for declared trigger and reporter entry-point groups."""

from __future__ import annotations

import tomllib
from importlib import invalidate_caches
from pathlib import Path

import pytest

from bernstein.core.trigger_sources.registry import Registry


def test_trigger_source_plugin_is_discoverable_after_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "fixture_trigger.py").write_text(
        "class JiraTriggerSource:\n    def normalize(self, raw_event):\n        return raw_event\n",
        encoding="utf-8",
    )
    _write_entry_points(tmp_path, "fixture_trigger", "bernstein.triggers", "jira = fixture_trigger:JiraTriggerSource")
    monkeypatch.syspath_prepend(str(tmp_path))
    invalidate_caches()

    registry = Registry()

    assert registry.get("jira").__name__ == "JiraTriggerSource"


def test_malformed_trigger_plugin_is_skipped_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write_entry_points(tmp_path, "broken_trigger", "bernstein.triggers", "broken = missing_trigger:BrokenTrigger")
    monkeypatch.syspath_prepend(str(tmp_path))
    invalidate_caches()

    registry = Registry()

    assert "artifact" in registry.list_names()
    assert "broken" not in registry.list_names()
    assert "Failed to load trigger entry-point 'broken'" in caplog.text


def test_declared_entry_point_groups_all_have_runtime_readers() -> None:
    """Every declared group is named somewhere under src/, so #4531 cannot recur."""
    root = Path(__file__).parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(pyproject["project"]["entry-points"])
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (root / "src").rglob("*.py"))

    unread = sorted(group for group in declared if group not in sources)

    assert not unread, f"declared with no runtime reader: {unread}"


def _write_entry_points(root: Path, distribution: str, group: str, entry: str) -> None:
    dist_info = root / f"{distribution}-0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "entry_points.txt").write_text(f"[{group}]\n{entry}\n", encoding="utf-8")
