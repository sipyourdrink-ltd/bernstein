"""Structural assertions on ``.github/workflows/contract-drift-autofix.yml``.

These tests guard the fork-PR fallback path. The workflow pushes the regen
commit directly to the source PR's head ref via ``git push --force-with-lease``.
That path works for same-repo PRs only; for fork PRs the default GITHUB_TOKEN
is read-only on the head repo, so the push step fails. The workflow handles
that case by detecting the fork up front and routing to the PR-comment path
instead.

If a refactor accidentally removes the fork-detect step or the comment
fallback, drift on fork PRs would silently fail with no operator signal.
Lock the structural shape so that regression is caught at unit-test time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


WORKFLOW = Path(".github/workflows/contract-drift-autofix.yml")


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(workflow_text: str) -> dict[str, object]:
    return yaml.safe_load(workflow_text)


@pytest.fixture(scope="module")
def autofix_steps(workflow: dict[str, object]) -> list[dict[str, object]]:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get("autofix")
    assert isinstance(job, dict), "expected an 'autofix' job"
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [s for s in steps if isinstance(s, dict)]


def test_workflow_file_exists() -> None:
    assert WORKFLOW.exists(), (
        "contract-drift-autofix workflow must live at .github/workflows/contract-drift-autofix.yml"
    )


def test_fork_detect_step_present(autofix_steps: list[dict[str, object]]) -> None:
    """A step with id 'forkcheck' must set is_fork=true|false."""
    forkcheck = next((s for s in autofix_steps if s.get("id") == "forkcheck"), None)
    assert forkcheck is not None, (
        "fork-detect step (id: forkcheck) is missing. EDGE-2 hardening requires "
        "the workflow to distinguish fork PRs (no push access) from same-repo PRs."
    )
    run = forkcheck.get("run", "")
    assert isinstance(run, str)
    assert "is_fork=true" in run and "is_fork=false" in run, (
        "forkcheck step must emit both is_fork=true and is_fork=false to GITHUB_OUTPUT"
    )


def test_inline_push_skips_forks(autofix_steps: list[dict[str, object]]) -> None:
    """The inline-push step must be gated on is_fork == 'false'."""
    push = next((s for s in autofix_steps if s.get("id") == "inline_push"), None)
    assert push is not None, "inline_push step is missing"
    cond = push.get("if", "")
    assert isinstance(cond, str)
    assert "is_fork == 'false'" in cond, (
        "inline_push must require steps.forkcheck.outputs.is_fork == 'false' to "
        "avoid attempting a push to a fork ref where GITHUB_TOKEN has no write access"
    )


def test_inline_push_uses_force_with_lease(autofix_steps: list[dict[str, object]]) -> None:
    """The inline-push step must use --force-with-lease for race safety."""
    push = next((s for s in autofix_steps if s.get("id") == "inline_push"), None)
    assert push is not None
    run = push.get("run", "")
    assert isinstance(run, str)
    assert "--force-with-lease" in run, (
        "inline push must use --force-with-lease to avoid clobbering a concurrent "
        "push from the PR author or another agent (EDGE-6 race-safety)"
    )


def test_inline_push_is_continue_on_error(autofix_steps: list[dict[str, object]]) -> None:
    """A lease conflict or branch-protection denial must NOT fail the job;
    the comment-fallback step covers that case."""
    push = next((s for s in autofix_steps if s.get("id") == "inline_push"), None)
    assert push is not None
    assert push.get("continue-on-error") is True, (
        "inline_push must continue-on-error so the PR-comment fallback can run when the push is rejected"
    )


def test_comment_fallback_fires_for_forks_or_failed_push(
    autofix_steps: list[dict[str, object]],
) -> None:
    """The comment-fallback step must trigger when is_fork == 'true' OR when
    inline_push failed (any reason)."""
    comment = next((s for s in autofix_steps if s.get("id") == "comment"), None)
    assert comment is not None, (
        "PR-comment fallback step (id: comment) is missing. Without it, fork PRs "
        "and lease-conflict same-repo PRs get no drift signal at all."
    )
    cond = comment.get("if", "")
    assert isinstance(cond, str)
    assert "is_fork == 'true'" in cond, "comment fallback must fire for fork PRs"
    assert "inline_push.outcome == 'failure'" in cond or "inline_push.outputs.pushed != 'true'" in cond, (
        "comment fallback must fire when inline_push failed"
    )


def test_step_actions_are_sha_pinned(autofix_steps: list[dict[str, object]]) -> None:
    """Every ``uses: <action>`` must be pinned to a 40-char SHA, never a tag.
    Tags are mutable; a malicious tag re-point would compromise the autofix bot.
    """
    import re

    sha_pattern = re.compile(r"@[0-9a-f]{40}(\s|$)")
    for step in autofix_steps:
        uses = step.get("uses")
        if not isinstance(uses, str):
            continue
        assert sha_pattern.search(uses), (
            f"action {uses!r} is not SHA-pinned. EDGE-3 hardening requires every "
            "third-party action to be pinned to a 40-char SHA. Pin via "
            "`uses: owner/action@<sha40> # <tag>`."
        )


def test_permissions_minimum_required(workflow: dict[str, object]) -> None:
    """The ``autofix`` job needs contents:write (to push) and
    pull-requests:write (to comment). issues:write is needed for the
    tracking-issue fallback.

    These are asserted on the job rather than on the workflow top level: the
    top level grants read only, so any job added to this file later starts
    without write and has to ask for it explicitly.
    """
    top = workflow.get("permissions", {})
    assert isinstance(top, dict)
    assert top.get("contents") == "read", "top level must stay read-only; grant write per job"
    assert "write" not in top.values(), f"no write scope belongs at the top level, found {top}"

    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get("autofix")
    assert isinstance(job, dict)
    perms = job.get("permissions", {})
    assert isinstance(perms, dict), "autofix must declare its own permissions"
    assert perms.get("contents") == "write", "needs contents:write to push regen commit"
    assert perms.get("pull-requests") == "write", "needs pull-requests:write for the comment-fallback path"
    assert perms.get("issues") == "write", "needs issues:write for the tracking-issue fallback"


def test_recursion_guard_on_bot_author(workflow: dict[str, object]) -> None:
    """The job-level ``if:`` must filter out bot-authored PRs so the workflow
    cannot trigger itself in a loop."""
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get("autofix")
    assert isinstance(job, dict)
    cond = job.get("if", "")
    assert isinstance(cond, str)
    assert "github-actions[bot]" in cond, "missing recursion guard: PRs authored by github-actions[bot] must be skipped"
