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

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli

if TYPE_CHECKING:
    from .fake_cli.conftest_adapters import FakeCLIHandle

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
        "Open an existing Git checkout",
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
    guide_text = guide.read_text(encoding="utf-8")
    example = (
        guide_text.split("A minimal `bernstein.yaml` looks like:", 1)[1].split("```yaml\n", 1)[1].split("\n```", 1)[0]
    )
    (tmp_path / "bernstein.yaml").write_text(example, encoding="utf-8")
    run_section = guide_text.split("## Step 4: Run your first goal", 1)[1]
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


def test_configured_first_run_completes_and_recaps(
    tmp_path: Path, unused_tcp_port: int, fake_cli_fixture: FakeCLIHandle
) -> None:
    """Run the guide through a real server, worker process, Git merge and recap."""
    project = tmp_path / "project"
    project.mkdir()
    profile = tmp_path / "home"
    profile.mkdir()
    git = shutil.which("git")
    assert git is not None
    fake_cli_fixture.configure(mode="first_run_greeting")
    env = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "WINDIR", "SYSTEMDRIVE")
        if key in os.environ
    }
    env.update(
        PATH=os.pathsep.join(
            (str(fake_cli_fixture.bin_dir), str(Path(git).parent), str(Path(sys.executable).parent), os.defpath)
        ),
        HOME=str(profile),
        USERPROFILE=str(profile),
        LANG="en_US.UTF-8",
        PYTHONUTF8="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Fixture",
        GIT_COMMITTER_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
        OPENAI_API_KEY="offline-fixture",
        OPENAI_BASE_URL="http://127.0.0.1:1/v1",
        NO_PROXY="localhost,127.0.0.1",
        BERNSTEIN_NO_SPLASH="1",
    )

    def run(command: list[str], *, check: bool = True) -> str:
        argv = [sys.executable, "-m", "bernstein", *command[1:]] if command[0] == "bernstein" else command
        result = subprocess.run(argv, cwd=project, env=env, timeout=150, capture_output=True, text=True)
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    guide = (Path(__file__).resolve().parents[2] / "docs/getting-started/first-run.md").read_text(encoding="utf-8")
    assert "bernstein" in run(["bernstein", "--version"]).lower()
    run(["git", "init", "--initial-branch=main"])
    run(["git", "commit", "--allow-empty", "-m", "init"])
    assert "git switch -c" in run(["bernstein", "init"])
    config = guide.split("A minimal `bernstein.yaml` looks like:", 1)[1].split("```yaml\n", 1)[1].split("\n```", 1)[0]
    (project / "bernstein.yaml").write_text(config, encoding="utf-8")
    steps = (
        guide.split("## Step 4: Run your first goal", 1)[1].split("```bash\n", 1)[1].split("\n```", 1)[0].splitlines()
    )
    run(shlex.split(steps[0], comments=True))
    # Installation-wide warnings can set doctor exit 1. The project result is independent.
    setup = json.loads(run(["bernstein", "doctor", "--json"], check=False))["project_setup"]
    assert setup["ready"] and setup["worker"] == "codex" and setup["planner"] == "none", setup

    try:
        # Noninteractive CI confirms the example and selects its own unused local port.
        output = run([*shlex.split(steps[1]), "--auto-approve", "--port", str(unused_tcp_port), "--wait", "90"])
        assert "Total tasks: 1" in output, output
        assert "Hello, world!" in (project / "src/greeting.py").read_text(encoding="utf-8")
        assert run(["git", "diff", "--name-only", "main...HEAD"]).strip() == "src/greeting.py"
        recap = guide.split("## Step 6: See what happened", 1)[1].split("```bash\n", 1)[1].split("\n```", 1)[0]
        report = json.loads(run([*shlex.split(recap), "--as-json"]))
        assert report["summary"] == {"total": 1, "completed": 1, "failed": 0, "success_rate": 100.0}
    finally:
        from bernstein.cli.run_confirm import _stop_demo_processes

        _stop_demo_processes(project)
