"""Structural assertions on the area-steward review-routing workflow.

``.github/workflows/area-steward-review.yml`` implements the routing half
of the CONTRIBUTING "Areas" clause: pull requests touching a steward's
area are requested for their review automatically. Stewards hold triage,
not write, so CODEOWNERS cannot do this - GitHub ignores code owners
without write access.

The failures this module exists to prevent
------------------------------------------
1. **A route that skips fork PRs.** Most docs PRs come from forks, where
   the plain ``pull_request`` event gets a read-only token that cannot
   request reviewers. The trigger must stay ``pull_request_target``.

2. **PR code executing in the privileged context.** ``pull_request_target``
   runs with a write-capable token. The job must never check out or
   otherwise execute anything from the PR: no checkout step, and no
   PR-controlled expression interpolated into a ``run:`` block.

3. **Scope creep on the token.** The workflow exists to make one API
   call. Workflow-level permissions must be default-deny and the job must
   hold exactly ``pull-requests: write``.

4. **The steward reviewing their own change.** A review request against
   the PR author 422s and would paint the run red on every PR the steward
   opens in their own area; the run must branch on author == steward.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "area-steward-review.yml"


@pytest.fixture(scope="module")
def raw() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(raw: str) -> dict:
    return yaml.safe_load(raw)


@pytest.fixture(scope="module")
def job(workflow: dict) -> dict:
    jobs = workflow["jobs"]
    assert len(jobs) == 1, "one routing job; a second job widens the privileged surface"
    return next(iter(jobs.values()))


def test_workflow_file_exists() -> None:
    assert _WORKFLOW.is_file()


def test_routes_fork_prs_too(workflow: dict) -> None:
    """Plain `pull_request` cannot request reviewers on fork PRs."""
    on = workflow.get("on", workflow.get(True))
    assert "pull_request_target" in on, (
        "fork PRs are most docs PRs; a pull_request trigger gets a read-only "
        "token there and the steward is never requested"
    )
    assert "pull_request" not in on


def test_scopes_to_the_docs_area(workflow: dict) -> None:
    on = workflow.get("on", workflow.get(True))
    assert on["pull_request_target"]["paths"] == ["docs/**"], (
        "the route is the Areas clause for the docs steward; widening the "
        "paths without a steward for the new area requests reviews nobody owns"
    )


def test_privileged_context_executes_nothing_from_the_pr(raw: str, job: dict) -> None:
    """pull_request_target runs write-capable; PR content must stay data."""
    for step in job["steps"]:
        uses = str(step.get("uses", ""))
        assert "checkout" not in uses, (
            "a checkout in a pull_request_target job is one `ref:` away from "
            "executing attacker-controlled code with a write token"
        )
    run_blocks = [step["run"] for step in job["steps"] if "run" in step]
    assert run_blocks, "the routing call itself is a run step"
    for block in run_blocks:
        assert "${{" not in block, (
            "PR-controlled expressions interpolated into run blocks are the "
            "classic pull_request_target injection; pass values through env"
        )


def test_token_scope_is_review_requests_only(workflow: dict, job: dict) -> None:
    assert workflow["permissions"] == {}, (
        "workflow-level permissions must be default-deny; the job re-asserts the one scope it needs"
    )
    assert job["permissions"] == {"pull-requests": "write"}


def test_the_steward_never_reviews_their_own_change(job: dict) -> None:
    """Requesting the PR author 422s; every steward-authored docs PR would go red."""
    env = job.get("env", {})
    assert "STEWARD" in env and "PR_AUTHOR" in env
    run = "\n".join(step["run"] for step in job["steps"] if "run" in step)
    assert re.search(r'"\$\{PR_AUTHOR\}"\s*=\s*"\$\{STEWARD\}"', run), (
        "the run must branch on author == steward before requesting the review"
    )


def test_the_steward_is_a_constant_not_an_expression(job: dict) -> None:
    """The reviewer must come from the trusted base ref, never from the event."""
    steward = str(job["env"]["STEWARD"])
    assert "${{" not in steward, "a steward name derived from the event lets the PR choose its own reviewer"
