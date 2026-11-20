"""Recompute the proposed autosync diff with the default branch's locked tooling."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path


def verify(trusted: Path, source: Path, sha: str, proposed: bytes) -> None:
    # The checkout supplies data only. Refuse links before any tool reads or
    # writes it, including a generated mirror whose path traverses a link.
    git = ["git", "-c", "core.hooksPath=/dev/null"]
    env = {
        "PATH": os.defpath,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C.UTF-8",
    }

    def output(command: list[str]) -> bytes:
        return subprocess.check_output(command, cwd=source, env=env)

    if output([*git, "rev-parse", "HEAD"]).decode().strip() != sha:
        raise ValueError("The verification checkout does not match the expected head")
    if output([*git, "status", "--porcelain", "--ignored"]):
        raise ValueError("The verification checkout is not clean")
    entries = output([*git, "ls-files", "--stage", "-z"]).split(b"\0")
    if any(entry and not entry.startswith((b"100644 ", b"100755 ")) for entry in entries):
        raise ValueError("Automatic verification requires a regular-file tree")

    # Import only the installed base implementation. Bypass CLI plugin
    # discovery, Python startup files, uv project discovery and PR Ruff config.
    ruff = shutil.which("ruff", path=str(Path(sys.executable).parent))
    if ruff is None:
        raise RuntimeError("The trusted environment does not contain Ruff")
    output(
        [
            sys.executable,
            "-I",
            "-c",
            "from bernstein.cli.commands.agents_md_cmd import agents_md_sync; agents_md_sync()",
            "--workdir",
            str(source),
            "--default-branch",
            os.environ["DEFAULT_BRANCH"],
        ]
    )
    for args in (["check", "--fix", "--unsafe-fixes"], ["format"]):
        output([ruff, *args, "--no-cache", "--config", str(trusted / "pyproject.toml"), "."])
    output([*git, "add", "-A"])
    diff = [*git, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames", "--full-index", "--binary", sha]
    with subprocess.Popen(diff, cwd=source, env=env, stdout=subprocess.PIPE) as proc:
        assert proc.stdout is not None
        expected = proc.stdout.read(len(proposed) + 1)
        if not expected or expected != proposed:
            proc.kill()
            raise ValueError("Proposed changes differ from the independently regenerated autosync diff")
        if proc.wait() != 0:
            raise RuntimeError("Cannot read the independently regenerated diff")


if __name__ == "__main__":
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    verify(
        workspace / "trusted",
        workspace / "source",
        os.environ["PR_HEAD_SHA"],
        base64.b64decode(os.environ["PATCH"], validate=True),
    )
