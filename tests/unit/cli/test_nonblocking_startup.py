"""``_background_startup`` must never block the bare ``cli()`` path (gh-6052).

``main.cli`` used to collect the startup thread with
``_splash_future.result(timeout=10)``.  On a machine where CLI agent discovery
takes longer than ten seconds that raised ``TimeoutError`` before the run
callback was ever reached, so ``bernstein -g GOAL --plan-only`` died with a
traceback instead of rendering a plan.  The startup results are no longer read
downstream, so the join buys nothing and only adds a crash surface.
"""

from __future__ import annotations

import os
import threading
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import bernstein.cli.main as main_mod


def test_slow_startup_does_not_block_plan_only(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow startup thread must not raise TimeoutError or skip the run callback."""
    release = threading.Event()
    recorded: dict[str, object] = {}

    def slow_startup(_workdir: object) -> dict[str, object]:
        release.wait(timeout=60)
        return {"agents": [], "task_count": 0}

    def fake_run_callback(**kwargs: object) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(main_mod, "_background_startup", slow_startup)
    with patch("bernstein.cli.splash_screen.splash", return_value=False):
        with patch.object(main_mod.run, "callback", fake_run_callback):
            runner = CliRunner()
            cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                result = runner.invoke(
                    main_mod.cli,
                    ["-g", "fix the failing test", "--plan-only"],
                    catch_exceptions=False,
                )
            finally:
                os.chdir(cwd)
                release.set()

    assert result.exit_code == 0, result.output
    assert recorded.get("plan_only") is True
    assert recorded.get("goal") == "fix the failing test"
