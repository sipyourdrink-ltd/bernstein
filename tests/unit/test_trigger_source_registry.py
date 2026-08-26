"""Regression coverage for declared trigger and reporter entry-point groups."""

from __future__ import annotations

import ast
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
    """Every declared group has a runtime ``entry_points(group=...)`` reader."""
    root = Path(__file__).parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(pyproject["project"]["entry-points"])
    readers = {group for path in (root / "src").rglob("*.py") for group in _entry_point_groups_read_by(path)}

    unread = sorted(declared - readers)

    assert not unread, f"declared with no runtime reader: {unread}"


def _write_entry_points(root: Path, distribution: str, group: str, entry: str) -> None:
    dist_info = root / f"{distribution}-0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "entry_points.txt").write_text(f"[{group}]\n{entry}\n", encoding="utf-8")


def _entry_point_groups_read_by(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    string_names = {
        target.id: value.value
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target, value in _assignment_targets_and_values(node)
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str)
    }
    string_names.update(_entry_point_group_defaults(tree, string_names))
    groups: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_entry_points_call(node.func):
            continue
        group = next((keyword.value for keyword in node.keywords if keyword.arg == "group"), None)
        if isinstance(group, ast.Constant) and isinstance(group.value, str):
            groups.add(group.value)
        elif isinstance(group, ast.Name) and group.id in string_names:
            groups.add(string_names[group.id])
    return groups


def _assignment_targets_and_values(node: ast.Assign | ast.AnnAssign) -> list[tuple[ast.expr, ast.expr | None]]:
    if isinstance(node, ast.Assign):
        return [(target, node.value) for target in node.targets]
    return [(node.target, node.value)]


def _entry_point_group_defaults(tree: ast.Module, string_names: dict[str, str]) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        positional_defaults = node.args.defaults
        if positional_defaults:
            for parameter, value in zip(positional[-len(positional_defaults) :], positional_defaults, strict=True):
                if isinstance(value, ast.Name) and value.id in string_names:
                    defaults[parameter.arg] = string_names[value.id]
        for parameter, value in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
            if isinstance(value, ast.Name) and value.id in string_names:
                defaults[parameter.arg] = string_names[value.id]
    return defaults


def _is_entry_points_call(function: ast.expr) -> bool:
    return (isinstance(function, ast.Name) and function.id == "entry_points") or (
        isinstance(function, ast.Attribute) and function.attr == "entry_points"
    )
