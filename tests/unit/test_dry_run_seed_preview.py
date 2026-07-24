"""``run --dry-run`` on a seed previews the plan and validates like a real run.

Before this fix ``_show_dry_run_plan`` ignored the seed and fetched tasks from a
task server that ``--dry-run`` never started, so a seed preview printed nothing
and failed with a "Task server not running" error (issue #2800). It also
accepted seeds a real run rejects, because it never called ``parse_seed``
(issue #2807). These tests pin the seed-synth path and the shared validation.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from bernstein.cli import run_bootstrap


def _patched_console(monkeypatch: Any) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(run_bootstrap, "console", Console(file=buf, force_terminal=False, width=200))
    return buf


def test_dry_run_previews_seed_without_server(monkeypatch: Any, tmp_path: Path) -> None:
    """A valid seed renders a scheduling plan with no running task server."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: Build a widget\n")

    # Any attempt to reach the task server would be a bug: fail loudly if hit.
    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("dry-run must not query the task server for a seed")

    monkeypatch.setattr(run_bootstrap.httpx, "get", _boom)
    buf = _patched_console(monkeypatch)

    run_bootstrap._show_dry_run_plan(
        workdir=tmp_path,
        plan_file=None,
        goal=None,
        seed_file=str(seed),
        model_override=None,
        cli=None,
    )

    output = buf.getvalue()
    assert "Task server not running" not in output
    assert "Dry-Run Scheduling Plan" in output
    assert "manager" in output


def test_dry_run_rejects_unselectable_cli_seed(monkeypatch: Any, tmp_path: Path) -> None:
    """A seed a real run rejects (unselectable ``cli:``) is rejected here too."""
    seed = tmp_path / "bernstein.yaml"
    seed.write_text("goal: Build a widget\ncli: mock\n")
    _patched_console(monkeypatch)

    with pytest.raises(SystemExit):
        run_bootstrap._show_dry_run_plan(
            workdir=tmp_path,
            plan_file=None,
            goal=None,
            seed_file=str(seed),
            model_override=None,
            cli=None,
        )
