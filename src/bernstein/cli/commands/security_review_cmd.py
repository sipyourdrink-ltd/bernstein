"""``bernstein security-review``: pattern-scan a diff for security issues.

Runs the regex catalogue in :mod:`bernstein.plugins.security_review` over a
unified diff and reports hardcoded secrets, unsafe ``eval``/``exec``, shell
injection, weak crypto, path traversal, SQL injection, and unsafe
deserialization.

The diff can come from three places, checked in this order:

* ``--diff-file PATH`` - read a saved diff (``-`` means stdin).
* ``TASK_ID``          - the diff an agent produced for that task, resolved the
  same way ``bernstein diff`` resolves it (worktree, branch, or merge commit).
* neither              - ``git diff`` against ``--base`` in ``--workdir``.

Exit codes: ``0`` clean or advisory-only, ``1`` at least one critical/high
finding (so it can gate a pre-commit hook or CI step), ``2`` no diff to scan.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _read_diff_file(diff_file: str) -> str:
    """Return diff text from a file path, or stdin when *diff_file* is ``-``."""
    if diff_file == "-":
        return sys.stdin.read()
    return Path(diff_file).read_text(encoding="utf-8")


def _git_diff(workdir: Path, base: str) -> str:
    """Return ``git diff`` output for *workdir* against *base*.

    Falls back to the unstaged working-tree diff when *base* is not a known
    revision (fresh repo, detached history, missing default branch).
    """
    for args in ([f"{base}...HEAD"], []):
        try:
            out = subprocess.run(
                ["git", "diff", *args],
                cwd=workdir,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout
    return ""


def _resolve_diff_text(task_id: str | None, diff_file: str | None, workdir: Path, base: str) -> str:
    """Resolve the diff text to scan from the operator's inputs."""
    if diff_file:
        return _read_diff_file(diff_file)
    if task_id:
        from bernstein.cli.commands.diff_cmd import _load_agents, resolve_diff

        return resolve_diff(task_id, workdir, _load_agents(workdir), base).diff_text
    return _git_diff(workdir, base)


@click.command("security-review")
@click.argument("task_id", required=False, default=None)
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(),
    help="Project root (parent of .sdd/).",
)
@click.option("--base", default="main", show_default=True, help="Base revision to diff against.")
@click.option(
    "--diff-file",
    default=None,
    type=click.Path(),
    help="Scan a saved diff instead of resolving one ('-' reads stdin).",
)
@click.option("--as-json", "as_json", is_flag=True, default=False, help="Emit findings as JSON.")
@click.option(
    "--fail-on-any",
    is_flag=True,
    default=False,
    help="Exit non-zero on any finding, not just critical/high.",
)
def security_review_cmd(
    task_id: str | None,
    workdir: str,
    base: str,
    diff_file: str | None,
    as_json: bool,
    fail_on_any: bool,
) -> None:
    """Scan a diff for security issues without calling an LLM.

    \b
    Examples:
      bernstein security-review                       # working tree vs main
      bernstein security-review 90307ac2              # what one agent changed
      git diff | bernstein security-review --diff-file -
    """
    from bernstein.plugins.security_review import (
        format_security_review,
        run_security_review,
        summarize_security_review,
    )

    root = Path(workdir).resolve()
    diff_text = _resolve_diff_text(task_id, diff_file, root, base)

    if not diff_text.strip():
        if as_json:
            click.echo(json.dumps({"total_findings": 0, "by_severity": {}, "blocked": False, "findings": []}))
        else:
            console.print("[yellow]Nothing to review:[/yellow] no diff resolved.")
        raise SystemExit(2)

    results = run_security_review(diff_text)
    summary = summarize_security_review(results)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "total_findings": summary.total_findings,
                    "by_severity": summary.by_severity,
                    "blocked": summary.blocked,
                    "findings": [
                        {
                            "file": r.file,
                            "severity": r.severity,
                            "pattern": r.pattern_name,
                            "description": r.description,
                            "line_range": list(r.line_range) if r.line_range else None,
                            "suggestion": r.suggestion,
                        }
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    else:
        console.print(format_security_review(results))

    if summary.blocked or (fail_on_any and summary.total_findings > 0):
        raise SystemExit(1)
