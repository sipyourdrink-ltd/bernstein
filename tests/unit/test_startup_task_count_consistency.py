"""Startup cost lines must not invent task counts.

Regression tests for issue #2748: one startup screen showed three different
task counts because two emitters substituted a hardcoded placeholder (5)
when the real count was not yet known.

Contract pinned here:

* The preflight cost line only prints a count when it comes from the plan
  file or the synced backlog, and that count is the one displayed.
* When the count is unknown (inline goal before planning, unreadable plan,
  empty backlog), the line says so and shows a per-task rate; it never
  prints a fabricated count.
* The bootstrap cost line derives its count from the synced/submitted
  backlog count and never falls back to a hardcoded number.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import patch

import bernstein.core.orchestration.bootstrap as bootstrap_mod
from bernstein.cli.run_preflight import (
    _emit_preflight_runtime_warnings,
    _estimate_run_preview,
    _estimate_task_count,
    console,
)

_COUNT_RE = re.compile(r"\d+\s+task")


# ---------------------------------------------------------------------------
# _estimate_task_count: unknown means None, never a placeholder
# ---------------------------------------------------------------------------


def test_goal_only_task_count_is_unknown(tmp_path: Path) -> None:
    """An inline goal has no plan yet - the count must be unknown, not 5."""
    assert _estimate_task_count(tmp_path, None, "ship a feature") is None


def test_unreadable_plan_task_count_is_unknown(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text("name: Demo\n", encoding="utf-8")
    with patch("bernstein.cli.run_preflight.load_plan_from_yaml", side_effect=ValueError("bad plan")):
        assert _estimate_task_count(tmp_path, plan_file, None) is None


def test_empty_backlog_task_count_is_unknown(tmp_path: Path) -> None:
    assert _estimate_task_count(tmp_path, None, None) is None


def test_plan_file_task_count_is_the_plan_count(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text("name: Demo\n", encoding="utf-8")
    with patch("bernstein.cli.run_preflight.load_plan_from_yaml", return_value=[object(), object(), object()]):
        assert _estimate_task_count(tmp_path, plan_file, None) == 3


# ---------------------------------------------------------------------------
# Preflight cost line
# ---------------------------------------------------------------------------


def test_preflight_cost_line_count_matches_plan_count(tmp_path: Path) -> None:
    """The printed count is exactly the plan count - no other number."""
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text("name: Demo\n", encoding="utf-8")
    with patch("bernstein.cli.run_preflight.load_plan_from_yaml", return_value=[object(), object(), object()]):
        estimate = _estimate_run_preview(
            workdir=tmp_path,
            plan_file=plan_file,
            goal=None,
            seed_file=None,
            model_override="sonnet",
        )
    assert estimate.task_count == 3

    with console.capture() as cap:
        _emit_preflight_runtime_warnings(
            workdir=tmp_path,
            estimate=estimate,
            auto_approve=True,
            quiet=False,
        )
    out = cap.get()
    counts = _COUNT_RE.findall(out)
    assert counts, f"expected a task count in output: {out!r}"
    assert all(c.split()[0] == "3" for c in counts), out


def test_preflight_cost_line_says_unknown_instead_of_inventing(tmp_path: Path) -> None:
    """Goal-only run: no count is printed, the line says it is not planned yet."""
    estimate = _estimate_run_preview(
        workdir=tmp_path,
        plan_file=None,
        goal="ship a feature",
        seed_file=None,
        model_override="sonnet",
    )
    assert estimate.task_count is None

    with console.capture() as cap:
        _emit_preflight_runtime_warnings(
            workdir=tmp_path,
            estimate=estimate,
            auto_approve=True,
            quiet=False,
        )
    out = cap.get()
    assert not _COUNT_RE.search(out), f"no count should be printed when unknown: {out!r}"
    assert "not yet planned" in out


# ---------------------------------------------------------------------------
# Bootstrap cost line
# ---------------------------------------------------------------------------


def test_bootstrap_cost_uses_submitted_count() -> None:
    text = bootstrap_mod._describe_cost_estimate(3, "sonnet")
    assert "3 task(s)" in text
    assert "5" not in text.split("$")[0]  # no stray placeholder before the price
    counts = _COUNT_RE.findall(text)
    assert all(c.split()[0] == "3" for c in counts), text


def test_bootstrap_cost_unknown_count_without_model() -> None:
    text = bootstrap_mod._describe_cost_estimate(0, None)
    assert "no model configured" in text
    assert "pending planning" in text
    assert not _COUNT_RE.search(text), text


def test_bootstrap_cost_unknown_count_with_model_shows_per_task_rate() -> None:
    text = bootstrap_mod._describe_cost_estimate(0, "sonnet")
    assert "per task" in text
    assert "pending planning" in text
    assert not _COUNT_RE.search(text), text


def test_bootstrap_has_no_hardcoded_placeholder_count() -> None:
    """The `backlog_count if backlog_count > 0 else 5` pattern must not return."""
    source = inspect.getsource(bootstrap_mod)
    assert "else 5" not in source
