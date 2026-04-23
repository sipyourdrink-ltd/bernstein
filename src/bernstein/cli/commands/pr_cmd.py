"""``bernstein pr`` — open a pull request from a completed session.

This command reads the newest (or a specific) session's wrap-up state,
derives a conventional-commit title, composes a markdown body with the
janitor quality-gate verdict and cost breakdown, pushes the session
branch if needed, and calls ``gh pr create`` to open the PR.

All pure logic lives in :mod:`bernstein.core.integrations.pr_gen`; this
module is a thin click wrapper that also handles subprocess calls to
``git`` and ``gh``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import click

from bernstein.core.integrations.pr_gen import (
    SessionSummary,
    build_pr_body,
    build_pr_title,
    load_session_summary,
)

_GH_MISSING_HINT = (
    "Could not find the `gh` CLI on PATH. Install it with `brew install gh` "
    "(macOS) or see https://cli.github.com/ for other platforms."
)


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git`` with captured output and a 30-second timeout.

    Args:
        args: Arguments to pass after ``git`` (e.g. ``["diff", "--stat"]``).
        cwd: Working directory for the subprocess.

    Returns:
        The completed process; callers inspect ``returncode`` and
        ``stdout`` / ``stderr``.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _current_branch(cwd: Path) -> str:
    """Return the current git branch, or ``"HEAD"`` when detached."""
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if result.returncode != 0:
        return "HEAD"
    return result.stdout.strip() or "HEAD"


def _diff_stat(cwd: Path, base: str, head: str) -> str:
    """Return ``git diff --stat base..head`` (empty string on failure)."""
    result = _run_git(["diff", "--stat", f"{base}..{head}"], cwd=cwd)
    if result.returncode != 0:
        return ""
    return result.stdout


def _enrich_summary_with_git(summary: SessionSummary, cwd: Path) -> SessionSummary:
    """Fill in the branch + diff-stat from git when the state file lacked them.

    Args:
        summary: The summary loaded from persisted state.
        cwd: Repository root.

    Returns:
        A new :class:`SessionSummary` with git-derived fields populated
        when they were missing from the original.
    """
    from dataclasses import replace
    from typing import cast

    branch = summary.branch if summary.branch not in ("", "HEAD") else _current_branch(cwd)
    diff_stat = summary.diff_stat or _diff_stat(cwd, summary.base_branch, branch)
    if branch == summary.branch and diff_stat == summary.diff_stat:
        return summary

    return cast("SessionSummary", replace(summary, branch=branch, diff_stat=diff_stat))


def _push_branch(branch: str, cwd: Path) -> tuple[bool, str]:
    """Push ``branch`` to ``origin`` with upstream tracking set.

    Args:
        branch: Branch name to push.
        cwd: Repository root.

    Returns:
        ``(ok, message)``; ``message`` is stderr on failure, otherwise empty.
    """
    result = _run_git(["push", "--set-upstream", "origin", branch], cwd=cwd)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    return True, ""


def _gh_pr_create(
    *,
    title: str,
    body: str,
    head: str,
    base: str,
    draft: bool,
    cwd: Path,
) -> tuple[bool, str]:
    """Invoke ``gh pr create`` and return ``(ok, url_or_error)``."""
    cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--head",
        head,
        "--base",
        base,
    ]
    if draft:
        cmd.append("--draft")

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)

    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    return True, result.stdout.strip()


@click.command("pr")
@click.option(
    "--session-id",
    "session_id",
    default=None,
    help="Session to publish. Defaults to the most-recent completed session.",
)
@click.option(
    "--base",
    "base",
    default="main",
    show_default=True,
    help="Base branch for the pull request.",
)
@click.option(
    "--title",
    "title_override",
    default=None,
    help="Override the auto-generated PR title.",
)
@click.option(
    "--draft",
    is_flag=True,
    default=False,
    help="Open the pull request as a draft.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print the would-be title and body without calling gh.",
)
@click.option(
    "--no-push",
    "no_push",
    is_flag=True,
    default=False,
    help="Skip `git push`; assume the branch is already on origin.",
)
def pr_cmd(
    session_id: str | None,
    base: str,
    title_override: str | None,
    draft: bool,
    dry_run: bool,
    no_push: bool,
) -> None:
    """Open a GitHub pull request from a completed Bernstein session.

    The command is safe to re-run: on ``--dry-run`` it never touches the
    network, and when ``gh`` is missing it exits with a helpful message
    instead of a traceback.
    """
    workdir = Path.cwd()

    summary = load_session_summary(session_id, workdir=workdir, base_branch=base)
    summary = _enrich_summary_with_git(summary, workdir)

    title = title_override or build_pr_title(summary.goal or summary.session_id, summary.primary_role)
    body = build_pr_body(summary)

    if dry_run:
        click.echo(f"Title: {title}")
        click.echo("Body:")
        click.echo(body)
        return

    if shutil.which("gh") is None:
        raise click.ClickException(_GH_MISSING_HINT)

    if not no_push:
        ok, push_err = _push_branch(summary.branch, workdir)
        if not ok:
            raise click.ClickException(f"git push failed: {push_err}")

    ok, message = _gh_pr_create(
        title=title,
        body=body,
        head=summary.branch,
        base=summary.base_branch,
        draft=draft,
        cwd=workdir,
    )
    if not ok:
        raise click.ClickException(f"gh pr create failed: {message}")

    click.echo(message)
