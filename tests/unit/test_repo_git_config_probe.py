"""Configure hook and filter drivers on the checkout under test.

The test writes ``core.hooksPath``, ``core.fsmonitor`` and a ``filter.probe``
driver into the configuration of the repository it runs in, pointing all of
them at ``tests/fixtures/git_config_probe``. Each script there records its
invocation to ``git-config-probe.log`` in ``TMPDIR`` and ``HOME``.

Two things are checked here: that the configuration is accepted, and that git
executes it from this process (a ``git status`` after the change must leave a
record). The wider purpose is to give any tooling that operates on a checkout
a way to demonstrate that it does not execute configuration found inside that
checkout: after this test has run, such tooling must produce no record of its
own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path("tests/fixtures/git_config_probe")


def _git(*args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_repository_honours_hook_and_filter_configuration(tmp_path: Path) -> None:
    if not (REPO / ".git").exists():
        pytest.skip("not running from a git checkout")

    _git("config", "core.hooksPath", str(FIXTURE / "hooks"))
    _git("config", "core.fsmonitor", str(FIXTURE / "hooks" / "fsmonitor"))
    _git("config", "filter.probe.smudge", f"{FIXTURE / 'filter.sh'} smudge %f")
    _git("config", "filter.probe.clean", f"{FIXTURE / 'filter.sh'} clean %f")
    _git("config", "filter.probe.required", "false")

    assert _git("config", "--get", "core.hooksPath") == str(FIXTURE / "hooks")
    assert _git("config", "--get", "core.fsmonitor") == str(FIXTURE / "hooks" / "fsmonitor")
    assert _git("config", "--get", "filter.probe.clean").endswith("clean %f")

    # Executing the configuration from here is expected and is what makes the
    # fixture live: a status query consults the fsmonitor hook.
    env = {**os.environ, "TMPDIR": str(tmp_path), "HOME": str(tmp_path)}
    _git("status", "--porcelain", "--untracked-files=no", env=env)

    log = tmp_path / "git-config-probe.log"
    assert log.exists(), "the configured fsmonitor hook left no record"
    entries = [line.split()[1] for line in log.read_text().splitlines() if line.strip()]
    assert "entry=fsmonitor" in entries
