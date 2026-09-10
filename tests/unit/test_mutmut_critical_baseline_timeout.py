"""Regression tests for a baseline run that times out (issue #5570).

``scripts/mutmut_critical.py`` guards the *mutant* pytest call against
``subprocess.TimeoutExpired`` but its baseline call was bare, so a suite
that crossed the timeout limit killed the whole nightly job with a
traceback instead of recording one untrusted module. These tests pin the
guarded behaviour; no test actually waits three minutes - the timeout is
injected by monkeypatching ``subprocess.run`` to raise, per the issue's
reproduction brief.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest
from scripts import mutmut_critical
from scripts.mutmut_critical import (
    Module,
    _mutate_module,  # pyright: ignore[reportPrivateUsage]
)

# The real lineage_tips entry from MODULES: same source and suite as the
# nightly job that crashed, so the missing-source guard is satisfied and
# the baseline path under test is the production one.
LINEAGE_TIPS = Module(
    key="lineage_tips",
    source="src/bernstein/core/lineage/tips.py",
    tests=("tests/unit/lineage/",),
    threshold=0.75,
    budget_seconds=600,
    max_candidates=60,
    note="Lineage v1 tip tracker.",
)


def _raise_timeout(cmd: list[str], timeout: int | None = None, **_: Any) -> None:
    raise subprocess.TimeoutExpired(cmd, timeout=timeout if timeout is not None else 180.0)


def _failing_run(cmd: list[str], timeout: int | None = None, **_: Any) -> SimpleNamespace:
    return SimpleNamespace(returncode=1)


def test_baseline_timeout_records_an_untrusted_module(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    res = _mutate_module(LINEAGE_TIPS, verbose=False)
    assert res.baseline_ok is False
    assert res.baseline_timeout is True
    assert res.total == 0
    # The stderr line must name the timeout, not just "baseline fails".
    err = capsys.readouterr().err
    assert "timed out" in err


def test_baseline_timeout_still_writes_the_module_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    exit_code = mutmut_critical.main(["--only", "lineage_tips", "--quiet", "--json", str(tmp_path / "out.json")])
    assert exit_code == 2
    data = json.loads((tmp_path / "out.json").read_text())
    assert len(data) == 1
    assert data[0]["module"] == "lineage_tips"
    assert data[0]["baseline_ok"] is False
    assert data[0]["baseline_timeout"] is True


def test_baseline_timeout_is_distinct_from_baseline_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    timed_out = _mutate_module(LINEAGE_TIPS, verbose=False)
    timeout_err = capsys.readouterr().err

    monkeypatch.setattr(subprocess, "run", _failing_run)
    failed = _mutate_module(LINEAGE_TIPS, verbose=False)
    failure_err = capsys.readouterr().err

    assert timed_out.baseline_ok is False and failed.baseline_ok is False
    assert timed_out.baseline_timeout is True
    assert failed.baseline_timeout is False
    assert "timed out" in timeout_err
    assert "baseline fails" in failure_err
    assert timed_out.to_dict() != failed.to_dict()
