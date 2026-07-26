"""Every workflow that opens a pull request must use a triggering token.

A pull request created with the Actions token (``secrets.GITHUB_TOKEN`` or the
equivalent ``github.token``) does not trigger workflows. Neither required
context ever reports on it, so branch protection holds it at BLOCKED while the
status rollup reads SUCCESS. Nothing is red, nothing is pending, and the only
way out is an operator closing it by hand - and a closed pull request cannot be
revived by force-pushing its branch, so the next fire opens a fresh one.

The damage is not the churn. An automation lane whose pull requests can never
merge is a regeneration that never lands: the artefact it exists to refresh
goes stale while the lane keeps reporting that it ran. That is the same shape
as a check that reports without checking.

This module enumerates the pull-request-opening steps **by discovery** rather
than from a list. A new automation lane that copies the old shape is caught by
the same assertion that catches the current ones, without anyone remembering to
extend a fixture.

Two step shapes count as opening a pull request:

* ``uses: peter-evans/create-pull-request@...`` - the token is ``with.token``;
* a ``run:`` block invoking ``gh pr create`` - the token is ``GH_TOKEN`` (or
  ``GITHUB_TOKEN``) resolved from the step, then the job, then the workflow.

Comment lines are stripped from ``run:`` blocks before matching, so a workflow
that only *describes* ``gh pr create`` in a comment - ``auto-release.yml``
explains why it stopped calling it - is not mistaken for one that calls it.

The last assertion here is the one that is easiest to get wrong. A reusable
workflow (``on: workflow_call``) sees only the secrets its caller passed:
``secrets.SOMETHING`` that was never declared and forwarded evaluates to the
empty string rather than raising. A ``||`` fallback then swallows it and the
lane silently degrades to the Actions token - the fix present in the file, and
absent at runtime. So a declared preference is only real when the callee
declares the secret and every caller forwards it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"

_CREATE_PR_ACTION = "peter-evans/create-pull-request"
_CREATE_PR_COMMAND = re.compile(r"\bgh\s+pr\s+create\b")

# The Actions token under either spelling. Both are suppressed as a workflow
# trigger; `github.token` is not a safe alternative to `secrets.GITHUB_TOKEN`.
_ACTIONS_TOKEN = re.compile(r"secrets\.GITHUB_TOKEN|github\.token")

# Token env vars `gh` reads, in the order it prefers them.
_GH_TOKEN_KEYS = ("GH_TOKEN", "GITHUB_TOKEN")


def _strip_comments(script: str) -> str:
    """Drop whole-line shell comments so prose cannot look like a call."""
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


def _as_env(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}  # type: ignore[union-attr]


def _resolve_gh_token(step: dict[str, Any], job: dict[str, Any], workflow: dict[str, Any]) -> str | None:
    """Return the token expression `gh` would use inside ``step``."""
    for scope in (step, job, workflow):
        env = _as_env(scope.get("env"))
        for key in _GH_TOKEN_KEYS:
            if key in env:
                return env[key]
    return None


def _workflow_files() -> list[Path]:
    return sorted(p for p in _WORKFLOW_DIR.glob("*.yml") if p.is_file())


def _pr_opening_steps() -> list[tuple[str, str, str, str | None]]:
    """Return ``(workflow, job, step, token_expression)`` for every PR opener."""
    found: list[tuple[str, str, str, str | None]] = []
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            continue
        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                label = str(step.get("name") or step.get("id") or f"step[{index}]")
                uses = str(step.get("uses", ""))
                if _CREATE_PR_ACTION in uses:
                    with_block = step.get("with")
                    token = None
                    if isinstance(with_block, dict):
                        raw_token = with_block.get("token")
                        token = None if raw_token is None else str(raw_token)
                    found.append((path.name, str(job_name), label, token))
                    continue
                script = step.get("run")
                if isinstance(script, str) and _CREATE_PR_COMMAND.search(_strip_comments(script)):
                    found.append(
                        (path.name, str(job_name), label, _resolve_gh_token(step, job, workflow)),
                    )
    return found


def test_workflow_directory_is_readable() -> None:
    assert _workflow_files(), "no workflow files discovered; the sweep would pass vacuously"


def test_discovery_finds_the_known_pull_request_lanes() -> None:
    """Guard the discovery itself: a broken matcher would pass everything."""
    lanes = {workflow for workflow, _job, _step, _token in _pr_opening_steps()}
    for expected in (
        "adapter-conformance-canary.yml",
        "auto-heal.yml",
        "bernstein-ci-fix.yml",
        "bernstein-issues-decompose.yml",
        "ci-topology-heal.yml",
        "coverage-ratchet-weekly.yml",
        "coverage-ratchet.yml",
        "docs-observability-snapshot.yml",
        "nightly-drift-sweep.yml",
        "review-bot-sweep.yml",
    ):
        assert expected in lanes, f"{expected} opens a pull request but discovery missed it"


def test_comment_only_mentions_are_not_counted_as_openers() -> None:
    """``auto-release.yml`` explains why it no longer calls ``gh pr create``."""
    raw = (_WORKFLOW_DIR / "auto-release.yml").read_text(encoding="utf-8")
    assert "gh pr create" in raw, "fixture drifted: auto-release.yml no longer mentions the command"
    lanes = {workflow for workflow, _job, _step, _token in _pr_opening_steps()}
    assert "auto-release.yml" not in lanes


@pytest.mark.parametrize(
    ("workflow", "job", "step", "token"),
    _pr_opening_steps(),
    ids=lambda value: str(value),
)
def test_pull_request_is_opened_with_a_triggering_token(
    workflow: str,
    job: str,
    step: str,
    token: str | None,
) -> None:
    assert token is not None, (
        f"{workflow} job {job!r} step {step!r} opens a pull request without naming a token. "
        "The default is the Actions token, which does not trigger workflows, so the pull "
        "request can never collect its required contexts."
    )

    without_actions_token = _ACTIONS_TOKEN.sub("", token)
    assert "secrets." in without_actions_token, (
        f"{workflow} job {job!r} step {step!r} opens a pull request with the Actions token only. "
        "A pull request created that way does not trigger workflows, so branch protection holds "
        "it at BLOCKED forever while the rollup reads SUCCESS. Prefer a configured PAT with a "
        "GITHUB_TOKEN fallback, e.g. "
        "${{ secrets.BERNSTEIN_AUTOSYNC_TOKEN || secrets.GITHUB_TOKEN }}."
    )

    actions_token = _ACTIONS_TOKEN.search(token)
    if actions_token is not None:
        assert "secrets." in token[: actions_token.start()], (
            f"{workflow} job {job!r} step {step!r} lists the Actions token before the triggering "
            "token. The Actions token may only be the fallback, never the preferred value."
        )


def _secret_names(token: str) -> set[str]:
    """Return the secret names a token expression reads."""
    return set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", token)) - {"GITHUB_TOKEN"}


def _declared_call_secrets(workflow: dict[str, Any]) -> set[str] | None:
    """Return declared ``workflow_call`` secrets, or None if not reusable."""
    triggers = workflow.get("on", workflow.get(True))
    if not isinstance(triggers, dict) or "workflow_call" not in triggers:
        return None
    call = triggers.get("workflow_call")
    declared = call.get("secrets") if isinstance(call, dict) else None
    return set(declared) if isinstance(declared, dict) else set()


def test_reusable_workflows_declare_the_secrets_their_pr_steps_read() -> None:
    """An undeclared secret reads as empty, so the `||` fallback hides the bug."""
    missing: list[str] = []
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            continue
        declared = _declared_call_secrets(workflow)
        if declared is None:
            continue
        for name, job, step, token in _pr_opening_steps():
            if name != path.name or token is None:
                continue
            for secret in sorted(_secret_names(token) - declared):
                missing.append(f"{path.name} job {job!r} step {step!r} reads secrets.{secret}")
    assert not missing, (
        "these reusable workflows read a secret they do not declare under "
        "on.workflow_call.secrets, so it evaluates to empty at runtime and the "
        f"pull request is opened with the Actions token after all: {missing}"
    )


def test_callers_forward_the_secrets_reusable_pr_lanes_declare() -> None:
    """Declaring the secret is only half of it; the caller has to pass it."""
    reusable: dict[str, set[str]] = {}
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            continue
        declared = _declared_call_secrets(workflow)
        if not declared:
            continue
        needed = {
            secret
            for name, _job, _step, token in _pr_opening_steps()
            if name == path.name and token is not None
            for secret in _secret_names(token)
        }
        if needed & declared:
            reusable[path.name] = needed & declared

    assert reusable, "no reusable pull-request lane found; this guard would pass vacuously"

    missing: list[str] = []
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            uses = str(job.get("uses", ""))
            callee = uses.rsplit("/", 1)[-1].split("@", 1)[0]
            if callee not in reusable:
                continue
            passed = job.get("secrets")
            if passed == "inherit":
                continue
            forwarded = set(passed) if isinstance(passed, dict) else set()
            for secret in sorted(reusable[callee] - forwarded):
                missing.append(f"{path.name} job {job_name!r} calls {callee} without forwarding secrets.{secret}")
    assert not missing, (
        "these callers do not forward a secret the callee's pull-request step reads, "
        f"so it evaluates to empty inside the reusable workflow: {missing}"
    )
