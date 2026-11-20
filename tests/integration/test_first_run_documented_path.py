"""The documented first-run setup and handoff to ``bernstein run``.

``docs/getting-started/first-run.md`` step 3 tells a new operator to run
``bernstein init`` in a fresh project and shows the output they should see.
This holds that promise to the code: the command is invoked exactly as the
docs write it, from an empty directory, and the strings the docs print are
asserted individually.

Asserting only on the exit code would pass on a command that prints nothing,
which is the failure mode this test exists to catch.

The first goal example must also get past branch preflight in a fresh repo.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli

pytestmark = pytest.mark.integration


def _cli(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bernstein", *args],
        cwd=workdir,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_init_succeeds_in_an_empty_directory(tmp_path: Path) -> None:
    """``bernstein init`` is the first command the docs ask for, and it works.

    The directory is empty and is deliberately not a git repository: the docs
    only require git before a *run* spawns worktree-isolated agents, not
    before ``init``.
    """
    result = _cli(tmp_path, "init")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


@pytest.mark.parametrize(
    "promised",
    [
        "Created bernstein.yaml",
        "Done. Next steps:",
        "Edit bernstein.yaml: set a goal",
        "git switch -c bernstein/first-run",
    ],
)
def test_init_prints_what_the_docs_promise(tmp_path: Path, promised: str) -> None:
    """Each line ``first-run.md`` shows for step 3 is really printed."""
    result = _cli(tmp_path, "init")

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert promised in result.stdout, (
        f"docs/getting-started/first-run.md promises {promised!r} but init printed:\n{result.stdout}"
    )


def test_init_creates_the_artifacts_the_docs_describe(tmp_path: Path) -> None:
    """The four things ``first-run.md`` says happened actually happened."""
    result = _cli(tmp_path, "init")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    assert (tmp_path / ".sdd" / "config.yaml").is_file()
    assert (tmp_path / "bernstein.yaml").is_file()
    assert (tmp_path / "templates").is_dir()
    assert ".sdd/runtime/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


def test_documented_first_goal_passes_branch_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute the First Run example until the CLI reaches its run preview."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH", raising=False)
    subprocess.run(["git", "init", "--initial-branch=main"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    init = _cli(tmp_path, "init")
    assert init.returncode == 0, init.stdout + init.stderr

    guide = Path(__file__).resolve().parents[2] / "docs/getting-started/first-run.md"
    run_section = guide.read_text(encoding="utf-8").split("## Step 4: Run your first goal", 1)[1]
    commands = run_section.split("```bash\n", 1)[1].split("\n```", 1)[0]

    # Stop just after the real branch check, before planning or starting a provider.
    with patch("bernstein.cli.run_bootstrap._estimate_run_preview", side_effect=SystemExit(0)) as preview:
        for command in commands.splitlines():
            args = shlex.split(command, comments=True)
            if not args:
                continue
            if args[0] == "git":
                subprocess.run(args, check=True, capture_output=True)
            else:
                assert args[0] == "bernstein"
                result = CliRunner().invoke(cli, args[1:])
                assert result.exit_code == 0, result.output
    preview.assert_called_once()
