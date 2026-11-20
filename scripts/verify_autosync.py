"""Recompute the proposed autosync diff with the default branch's locked tooling."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def ruff_inputs(root: Path) -> dict:
    """Identify every tracked settings file Ruff can discover, including extends."""
    root = root.resolve()
    names = ("pyproject.toml", "ruff.toml", ".ruff.toml")
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    files = {Path(name) for name in tracked if name}
    pending = [path for path in files if path.name in names]
    settings = {}
    while pending:
        path = pending.pop()
        if path in settings:
            continue
        with (root / path).open("rb") as stream:
            document = tomllib.load(stream)
        if path.name == "pyproject.toml":
            config = document.get("tool", {}).get("ruff")
            requires_python = document.get("project", {}).get("requires-python")
        else:
            config, requires_python = document, None
        if config is None and requires_python is None:
            continue
        settings[path] = (config, requires_python)
        if config and "extend" in config:
            # Ruff expands environment variables and home directories. Automatic
            # verification accepts only repository-relative, tracked includes.
            value = config["extend"]
            if Path(value).is_absolute() or "$" in value or "~" in value:
                raise ValueError("Ruff extends must refer to a tracked repository file")
            extended = (root / path.parent / value).resolve().relative_to(root)
            if extended not in files:
                raise ValueError("Ruff extends must refer to a tracked repository file")
            pending.append(extended)
    return settings


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

    if ruff_inputs(source) != ruff_inputs(trusted):
        raise ValueError("PR Ruff settings differ from the trusted configuration hierarchy")

    # Import only the installed base implementation. Bypass CLI plugin
    # discovery, Python startup files and uv project discovery. Ruff discovers
    # the verified hierarchy normally, including each nested project.
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
        output([ruff, *args, "--no-cache", "."])
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
