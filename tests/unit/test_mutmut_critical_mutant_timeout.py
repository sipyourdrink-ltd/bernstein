"""Regression tests for mutant timeout scoring and budget derivation (issue #5621).

``scripts/mutmut_critical.py`` formerly counted timed-out mutant runs as kills,
which inflated the kill rate of slow test suites. Furthermore, mutant runs used
a flat 180s constant timeout instead of deriving from the module's own
budget_seconds. These tests pin the fixed behaviour using mocked runs.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest
from scripts.mutmut_critical import (
    Module,
    _mutant_timeout_seconds,  # pyright: ignore[reportPrivateUsage]
    _mutate_module,  # pyright: ignore[reportPrivateUsage]
)

TEST_MODULE = Module(
    key="test_mod",
    source="src/bernstein/core/lineage/tips.py",
    tests=("tests/unit/lineage/",),
    threshold=0.75,
    budget_seconds=800,
    max_candidates=2,
    note="Test module for mutant timeout regression.",
)


def test_mutant_timeout_derived_from_module_budget() -> None:
    """Mutant run timeout must be derived from mod.budget_seconds floored at 180s."""
    mod_small = Module(key="small", source="s", tests=(), threshold=0.7, budget_seconds=400)
    # 400 // 4 = 100, floored at 180
    assert _mutant_timeout_seconds(mod_small) == 180

    mod_large = Module(key="large", source="s", tests=(), threshold=0.7, budget_seconds=1200)
    # 1200 // 4 = 300
    assert _mutant_timeout_seconds(mod_large) == 300


def test_timed_out_mutant_is_not_scored_as_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out mutant is recorded in timeouts, not in killed."""
    call_count = 0

    def _mock_run(cmd: list[str], timeout: int | None = None, **_: Any) -> SimpleNamespace:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Baseline run passes
            return SimpleNamespace(returncode=0)
        # Mutant run times out
        raise subprocess.TimeoutExpired(cmd, timeout=timeout if timeout is not None else 180.0)

    monkeypatch.setattr(subprocess, "run", _mock_run)
    res = _mutate_module(TEST_MODULE, verbose=False)

    assert res.baseline_ok is True
    assert res.baseline_timeout is False
    assert res.timeouts > 0
    assert res.killed == 0
    assert res.kill_rate == 0.0


def test_material_timeouts_mark_module_unmeasured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When timed-out mutants constitute >= 20% of candidates, module is unmeasured."""
    call_count = 0

    def _mock_run(cmd: list[str], timeout: int | None = None, **_: Any) -> SimpleNamespace:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SimpleNamespace(returncode=0)
        raise subprocess.TimeoutExpired(cmd, timeout=timeout if timeout is not None else 180.0)

    monkeypatch.setattr(subprocess, "run", _mock_run)
    res = _mutate_module(TEST_MODULE, verbose=False)

    assert res.unmeasured is True
    assert res.passed is False
    assert res.to_dict()["unmeasured"] is True
