"""Tests for the shared affected-test plan artifact."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"


@pytest.fixture(scope="module")
def planner() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("plan_affected_tests_under_test", SCRIPTS / "plan_affected_tests.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_records_one_pinned_range_and_sorted_paths(planner: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        planner,
        "discover_affected_files",
        lambda _base: [planner.ROOT / "tests/unit/test_b.py"],
    )
    monkeypatch.setattr(planner, "discover_whole_tree_guard_files", lambda: [Path("tests/unit/test_guard.py")])
    monkeypatch.setattr(planner, "_resolve", lambda rev: {"pr-base": "base-sha", "HEAD": "head-sha"}[rev])

    plan = planner.build_plan("pr-base")

    assert plan == {
        "version": 1,
        "base": "pr-base",
        "base_sha": "base-sha",
        "head_sha": "head-sha",
        "affected": ["tests/unit/test_b.py"],
        "guards": ["tests/unit/test_guard.py"],
    }


def test_only_first_shard_carries_whole_tree_guards(
    planner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(planner, "load_shard_durations", lambda _path: {})
    plan = {
        "version": 1,
        "base": "pr-base",
        "base_sha": "base-sha",
        "head_sha": "head-sha",
        "affected": ["tests/unit/test_a.py", "tests/unit/test_b.py"],
        "guards": ["tests/unit/test_guard.py"],
    }

    first = planner.select_shard(plan, "1/2", tmp_path / "durations.json")
    second = planner.select_shard(plan, "2/2", tmp_path / "durations.json")

    assert "tests/unit/test_guard.py" in first
    assert "tests/unit/test_guard.py" not in second
    assert sorted(set(first + second)) == [
        "tests/unit/test_a.py",
        "tests/unit/test_b.py",
        "tests/unit/test_guard.py",
    ]


def test_read_plan_rejects_a_missing_path_list(planner: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(
        '{"version":1,"base":"pr-base","base_sha":"base","head_sha":"head","guards":[]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported affected-test plan"):
        planner.read_plan(path)


def test_empty_required_selection_fails_closed(planner: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planner, "discover_affected_files", lambda _base: [])
    monkeypatch.setattr(planner, "discover_changed_files", lambda _base, diff_filter=None: ["src/bernstein/core/x.py"])
    monkeypatch.setattr(planner, "changed_files_require_tests", lambda _changed, _deleted: True)
    monkeypatch.setattr(planner, "_resolve", lambda rev: {"pr-base": "base-sha", "HEAD": "head-sha"}[rev])

    with pytest.raises(RuntimeError, match=r"base-sha\.\.\.head-sha"):
        planner.build_plan("pr-base")
