"""Tests for ComplexityAdvisor."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.complexity_advisor import (
    TIGHT_COUPLING_THRESHOLD,
    ComplexityAdvisor,
    ComplexityMode,
    _cross_file_dep_score,
    _imports_any,
)
from bernstein.core.models import Scope, Task

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(owned_files: list[str] | None = None, scope: Scope = Scope.LARGE) -> Task:
    return Task(
        id="t1",
        title="Test task",
        description="...",
        role="backend",
        scope=scope,
        owned_files=owned_files or [],
    )


# ---------------------------------------------------------------------------
# force_parallel override
# ---------------------------------------------------------------------------


def test_force_parallel_always_multi_agent(tmp_path: Path) -> None:
    task = _task(owned_files=["a.py", "b.py"])
    advice = ComplexityAdvisor().advise(task, workdir=tmp_path, force_parallel=True)
    assert advice.mode == ComplexityMode.MULTI_AGENT
    assert advice.force_parallel is True


# ---------------------------------------------------------------------------
# No owned files
# ---------------------------------------------------------------------------


def test_no_owned_files_multi_agent(tmp_path: Path) -> None:
    task = _task(owned_files=[])
    advice = ComplexityAdvisor().advise(task, workdir=tmp_path)
    assert advice.mode == ComplexityMode.MULTI_AGENT
    assert advice.file_count == 0


# ---------------------------------------------------------------------------
# File count threshold
# ---------------------------------------------------------------------------


def test_many_files_always_multi_agent(tmp_path: Path) -> None:
    # 5+ files → always multi-agent regardless of coupling
    files = [f"file{i}.py" for i in range(6)]
    for f in files:
        (tmp_path / f).write_text("x = 1", encoding="utf-8")
    task = _task(owned_files=files)
    advice = ComplexityAdvisor().advise(task, workdir=tmp_path)
    assert advice.mode == ComplexityMode.MULTI_AGENT
    assert advice.file_count == 6


def test_exactly_threshold_files_multi_agent(tmp_path: Path) -> None:
    # Exactly at threshold (5) → multi-agent
    files = [f"m{i}.py" for i in range(5)]
    for f in files:
        (tmp_path / f).write_text("x = 1", encoding="utf-8")
    task = _task(owned_files=files)
    advice = ComplexityAdvisor().advise(task, workdir=tmp_path)
    assert advice.mode == ComplexityMode.MULTI_AGENT


# ---------------------------------------------------------------------------
# Coupling detection
# ---------------------------------------------------------------------------


def test_tightly_coupled_files_single_agent(tmp_path: Path) -> None:
    # a.py imports b.py, b.py imports a.py → both files have internal imports
    (tmp_path / "alpha.py").write_text("import beta\n\nfoo = 1", encoding="utf-8")
    (tmp_path / "beta.py").write_text("import alpha\n\nbar = 2", encoding="utf-8")

    task = _task(owned_files=["alpha.py", "beta.py"])
    advice = ComplexityAdvisor().advise(task, workdir=tmp_path)

    assert advice.mode == ComplexityMode.SINGLE_AGENT
    assert advice.cross_file_dep_score >= TIGHT_COUPLING_THRESHOLD
    assert advice.file_count == 2


def test_loosely_coupled_files_multi_agent(tmp_path: Path) -> None:
    # Neither file imports the other → low coupling → multi-agent OK
    (tmp_path / "foo.py").write_text("import os\n\nfoo = 1", encoding="utf-8")
    (tmp_path / "bar.py").write_text("import sys\n\nbar = 2", encoding="utf-8")

    task = _task(owned_files=["foo.py", "bar.py"])
    advice = ComplexityAdvisor().advise(task, workdir=tmp_path)

    assert advice.mode == ComplexityMode.MULTI_AGENT
    assert advice.cross_file_dep_score < TIGHT_COUPLING_THRESHOLD


def test_one_way_import_half_coupling(tmp_path: Path) -> None:
    # Only one of two files imports the other → score = 0.5, which is exactly at threshold
    (tmp_path / "parent.py").write_text("from child import helper", encoding="utf-8")
    (tmp_path / "child.py").write_text("def helper(): pass", encoding="utf-8")

    score = _cross_file_dep_score(["parent.py", "child.py"], tmp_path)
    # parent imports child: 1 out of 2 files has internal imports → 0.5
    assert score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# _imports_any
# ---------------------------------------------------------------------------


def test_imports_any_detects_direct_import(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    src.write_text("import utils\n", encoding="utf-8")
    assert _imports_any(src, {"utils"}) is True


def test_imports_any_detects_from_import(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    src.write_text("from helpers import do_thing\n", encoding="utf-8")
    assert _imports_any(src, {"helpers"}) is True


def test_imports_any_misses_external(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    src.write_text("import os\nimport sys\n", encoding="utf-8")
    assert _imports_any(src, {"utils", "helpers"}) is False


def test_imports_any_syntax_error_returns_false(tmp_path: Path) -> None:
    src = tmp_path / "broken.py"
    src.write_text("def (broken: \n", encoding="utf-8")
    assert _imports_any(src, {"utils"}) is False


# ---------------------------------------------------------------------------
# should_auto_decompose integration
# ---------------------------------------------------------------------------


def test_should_auto_decompose_skips_when_single_agent_recommended(tmp_path: Path) -> None:
    """Tightly coupled LARGE task should NOT decompose (advisor says single-agent)."""
    (tmp_path / "alpha.py").write_text("import beta\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("import alpha\n", encoding="utf-8")

    task = _task(owned_files=["alpha.py", "beta.py"], scope=Scope.LARGE)

    from bernstein.core.task_lifecycle import should_auto_decompose

    assert should_auto_decompose(task, set(), workdir=tmp_path, force_parallel=False) is False


def test_should_auto_decompose_decomposes_when_force_parallel(tmp_path: Path) -> None:
    """force_parallel=True should override single-agent advice."""
    (tmp_path / "alpha.py").write_text("import beta\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("import alpha\n", encoding="utf-8")

    task = _task(owned_files=["alpha.py", "beta.py"], scope=Scope.LARGE)

    from bernstein.core.task_lifecycle import should_auto_decompose

    assert should_auto_decompose(task, set(), workdir=tmp_path, force_parallel=True) is True


def test_should_auto_decompose_decomposes_loosely_coupled_large(tmp_path: Path) -> None:
    """Loosely coupled LARGE task with no owned_files → decompose only when force_parallel."""
    task = _task(owned_files=[], scope=Scope.LARGE)

    from bernstein.core.task_lifecycle import should_auto_decompose

    # Default: disabled (modern 1M-context LLMs handle any scope)
    assert should_auto_decompose(task, set(), workdir=tmp_path) is False
    assert should_auto_decompose(task, set(), workdir=tmp_path, force_parallel=True) is True


def test_should_auto_decompose_no_workdir_decomposes_large(tmp_path: Path) -> None:
    """Without workdir, falls back to original scope=LARGE rule when forced."""
    task = _task(scope=Scope.LARGE)

    from bernstein.core.task_lifecycle import should_auto_decompose

    assert should_auto_decompose(task, set(), workdir=None) is False
    assert should_auto_decompose(task, set(), workdir=None, force_parallel=True) is True


def test_should_auto_decompose_skips_small_scope(tmp_path: Path) -> None:
    task = _task(scope=Scope.SMALL)

    from bernstein.core.task_lifecycle import should_auto_decompose

    assert should_auto_decompose(task, set(), workdir=tmp_path) is False
