"""Tests verifying UV_NO_SYNC and --no-sync isolation for test subprocesses (issue #4826).

When tests invoke `uv run` in a subprocess, `uv` defaults to reconciling the project
environment (.venv). In a parallel test runner (e.g. pytest or run_tests.py), this
can rewrite the active virtual environment that other tests are running from.
Setting `UV_NO_SYNC=1` and/or `--no-sync` ensures `uv run` acts solely as a launcher
without mutating or syncing the project .venv.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv binary not found on PATH",
)


def test_uv_run_with_no_sync_does_not_create_or_sync_project_venv(tmp_path: Path) -> None:
    """Invoking ``uv run --no-sync`` or with ``UV_NO_SYNC=1`` avoids project sync.

    Against a scratch directory declaring a non-installed dependency, ``uv run``
    without ``--no-sync`` would attempt to sync and fail or alter the environment,
    whereas ``--no-sync`` and ``UV_NO_SYNC=1`` executes cleanly without syncing.
    """
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "scratch-pkg"\nversion = "0.1.0"\ndependencies = ["nonexistent-package-xyz==99.99.99"]\n',
        encoding="utf-8",
    )

    # Without --no-sync or UV_NO_SYNC, uv run attempts to sync and fails resolving nonexistent package
    env_sync = {k: v for k, v in os.environ.items() if k != "UV_NO_SYNC"}
    env_sync["UV_PROJECT"] = str(proj)
    proc_sync = subprocess.run(
        ["uv", "run", "--python", sys.executable, "python", "-c", "print('should-not-run')"],
        cwd=proj,
        env=env_sync,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc_sync.returncode != 0
    assert "nonexistent-package-xyz" in proc_sync.stderr or "error" in proc_sync.stderr

    # With --no-sync and UV_NO_SYNC=1, uv run executes directly without syncing
    env_nosync = {**os.environ, "UV_NO_SYNC": "1", "UV_PROJECT": str(proj)}
    proc_nosync = subprocess.run(
        ["uv", "run", "--no-sync", "--python", sys.executable, "python", "-c", "import sys; print('isolated-ok')"],
        cwd=proj,
        env=env_nosync,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc_nosync.returncode == 0
    assert "isolated-ok" in proc_nosync.stdout
