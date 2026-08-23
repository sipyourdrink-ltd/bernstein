"""Volunteer PR submission: body builders, pacing state, and DCO sign-off.

Builds the PR body from a :class:`ResultBundle`, enforces one-open-PR-per-
project pacing, and adds a DCO ``Signed-off-by:`` trailer from the donor's
own git config.

The body builder is a pure function (no I/O) so it can be golden-tested.
The pacing state lives beside the budget ledger under
``~/.bernstein/volunteer/pacing/``. All ``gh`` calls route through the
injectable :data:`GhRunner` seam so non-GitHub forges can substitute later.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.git.git_pr import push_head_as
from bernstein.core.volunteer.claim import GhRunner, _default_runner, repo_slug

if TYPE_CHECKING:
    from bernstein.core.security.result_receipt_bundle import (
        GateResult,
        ResultBundle,
        TaskRef,
    )

logger = logging.getLogger(__name__)

__all__ = [
    "PacingError",
    "SubmissionError",
    "build_volunteer_pr_body",
    "build_volunteer_pr_title",
    "check_pacing",
    "read_dco_line",
    "submit_volunteer_pr",
]


class PacingError(RuntimeError):
    """A volunteer PR is already open for this (donor, project) pair."""


class SubmissionError(RuntimeError):
    """PR creation failed."""


# ---------------------------------------------------------------------------
# Pure body builders
# ---------------------------------------------------------------------------


def _format_gate_table(gates: tuple[GateResult, ...]) -> str:
    """Render gate results as a markdown table with command, exit code, and status."""
    if not gates:
        return "_No gates were configured for this task._"
    lines = ["| Command | Exit Code | Status |", "|---|---|---|"]
    for gate in gates:
        mark = "✅" if gate.exit_code == 0 else "❌"
        lines.append(f"| `{gate.command}` | {gate.exit_code} | {mark} |")
    return "\n".join(lines)


def build_volunteer_pr_body(
    bundle: ResultBundle,
    *,
    adapter_id: str,
    model_id: str,
    signed_off_by: str,
    bundle_digest: str,
) -> str:
    """Render the full markdown body for a volunteer pull request.

    Follows the section-always-present discipline from
    :func:`bernstein.core.integrations.pr_gen.build_pr_body` so tests can
    rely on section headers.  Trailing trailer lines are grep-able.

    Args:
        bundle: The signed result receipt bundle.
        adapter_id: Adapter that performed the work (e.g. ``"claude"``).
        model_id: Model that performed the work (e.g. ``"sonnet"``).
        signed_off_by: DCO sign-off line content (``"Name <email>"``).
        bundle_digest: SHA-256 of the bundle's canonical bytes.

    Returns:
        A markdown string ready to pass to ``gh pr create --body``.
    """
    issue_ref = f"issue #{bundle.task.issue_number}" if bundle.task.issue_number else "the tracked issue"
    repo_ref = bundle.task.repo

    parts: list[str] = [
        "## Summary",
        "",
        f"Automated volunteer submission via bernstein, addressing {issue_ref} on `{repo_ref}`.",
        "The gate results below were produced under the project's declared volunteer policy.",
        "",
        "## Gate Results",
        "",
        _format_gate_table(bundle.gates),
        "",
        "## Verification",
        "",
        f"- **Receipt digest:** `{bundle_digest}`",
        f"- **Manifest digest:** `{bundle.manifest_sha256}`",
        "- **Verify offline:** `bernstein receipt verify bundle.json`",
        "",
        "---",
        "",
        f"_Assisted-by: {adapter_id} ({model_id})_",
        f"Signed-off-by: {signed_off_by}",
    ]
    return "\n".join(parts)


def build_volunteer_pr_title(task: TaskRef) -> str:
    """Compose a conventional-commit pull-request title from a task reference.

    Follows :func:`bernstein.core.integrations.pr_gen.build_pr_title`'s shape
    but derives the outcome from the issue number rather than a goal string.
    """
    if task.issue_number is not None:
        outcome = f"resolve issue #{task.issue_number}"
    else:
        outcome = f"automated submission for {task.commit_sha[:12]}"
    return f"fix(volunteer): {outcome}"


# ---------------------------------------------------------------------------
# DCO sign-off
# ---------------------------------------------------------------------------


def read_dco_line(cwd: Path) -> str | None:
    """Read ``Signed-off-by`` content from the donor's git config.

    Returns ``"Name <email>"`` or ``None`` when name/email are unset.
    A submission with no sign-off is refused rather than silently omitted.
    """
    from bernstein.core.git.git_basic import run_git

    name_r = run_git(["config", "--get", "user.name"], cwd, timeout=5)
    email_r = run_git(["config", "--get", "user.email"], cwd, timeout=5)
    name = name_r.stdout.strip() if name_r.ok else ""
    email = email_r.stdout.strip() if email_r.ok else ""
    if not name or not email:
        return None
    return f"{name} <{email}>"


# ---------------------------------------------------------------------------
# Pacing state
# ---------------------------------------------------------------------------


def _pacing_dir() -> Path:
    return Path.home() / ".bernstein" / "volunteer" / "pacing"


def _pacing_path(slug: str) -> Path:
    safe_slug = slug.replace("/", "-")
    return _pacing_dir() / f"{safe_slug}.json"


def _read_pacing(slug: str) -> str | None:
    """Return the open PR URL for this project, or ``None``."""
    path = _pacing_path(slug)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return str(data.get("pr_url", "")) or None


def _write_pacing(slug: str, pr_url: str) -> None:
    path = _pacing_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pr_url": pr_url}, sort_keys=True), encoding="utf-8")


def _clear_pacing(slug: str) -> None:
    _pacing_path(slug).unlink(missing_ok=True)


def _check_pr_state(pr_url: str, runner: GhRunner) -> str | None:
    """Return ``'OPEN'``, ``'MERGED'``, ``'CLOSED'``, or ``None`` on error."""
    result = runner(["pr", "view", pr_url, "--json", "state"], None)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return str(data.get("state", "")).upper() or None


def check_pacing(slug: str, runner: GhRunner) -> None:
    """Refuse if a volunteer PR is already open for this project.

    Clears pacing state when the tracked PR is merged or closed, so the
    next submission can proceed.
    """
    existing = _read_pacing(slug)
    if not existing:
        return
    state = _check_pr_state(existing, runner)
    if state in ("MERGED", "CLOSED"):
        _clear_pacing(slug)
        return
    # ``None`` means we could not tell; be safe and refuse.
    raise PacingError(
        f"A volunteer PR is already open for {slug}: {existing}\nMerge or close it before submitting another."
    )


# ---------------------------------------------------------------------------
# Submission orchestration
# ---------------------------------------------------------------------------


def submit_volunteer_pr(
    *,
    bundle: ResultBundle,
    repo_url: str,
    branch: str,
    base: str = "main",
    draft: bool = True,
    runner: GhRunner | None = None,
    cwd: Path,
) -> str:
    """Push the branch and open a volunteer PR, enforcing pacing and DCO.

    Args:
        bundle: The signed result receipt bundle.
        repo_url: The project's repository URL (used for slug + pacing).
        branch: Local branch name to push.
        base: Target branch (default ``"main"``).
        draft: Open as a draft PR (recommended; the issue's open decision).
        runner: Injectable ``gh`` runner for testing.
        cwd: Repository working directory.

    Returns:
        The URL of the created PR.

    Raises:
        PacingError: A volunteer PR is already open for this project.
        SubmissionError: PR creation failed or DCO is missing.
    """
    runner = runner or _default_runner
    slug = repo_slug(repo_url) or repo_url

    # 1. Pacing check — refuses before any push or PR create.
    check_pacing(slug, runner)

    # 2. DCO sign-off — refuse if git config has no name/email.
    dco = read_dco_line(cwd)
    if not dco:
        raise SubmissionError(
            "Cannot submit: git config has no user.name/user.email. "
            "Set them to produce a valid DCO Signed-off-by trailer."
        )

    # 3. Build body and title.
    body = build_volunteer_pr_body(
        bundle,
        adapter_id=bundle.adapter_id,
        model_id=bundle.model_id,
        signed_off_by=dco,
        bundle_digest=bundle.digest,
    )
    title = build_volunteer_pr_title(bundle.task)

    # 4. Push branch to remote (donor's fork must already be configured as 'origin')

    push_result = push_head_as(cwd, branch)
    if not push_result.ok:
        raise SubmissionError(f"git push failed: {push_result.stderr.strip()}")

    # 5. Create PR via GhRunner (donor's own ``gh auth``).
    cmd = ["pr", "create", "--title", title, "--body", body, "--head", branch, "--base", base]
    if draft:
        cmd.append("--draft")
    result = runner(cmd, None)
    if result.returncode != 0:
        raise SubmissionError(f"gh pr create failed: {result.stderr.strip()}")

    pr_url = result.stdout.strip()
    _write_pacing(slug, pr_url)
    return pr_url
