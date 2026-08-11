"""Structural assertions on ``.github/workflows/bernstein-pr-review.yml``."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast

import pytest
import yaml

WORKFLOW = Path(".github/workflows/bernstein-pr-review.yml")
WorkflowStep = TypedDict(
    "WorkflowStep",
    {
        "env": object,
        "name": object,
        "run": object,
        "uses": object,
        "with": object,
    },
    total=False,
)


class Workflow(TypedDict, total=False):
    jobs: object


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(workflow_text: str) -> Workflow:
    loaded = yaml.safe_load(workflow_text)
    assert isinstance(loaded, dict)
    return cast(Workflow, loaded)


@pytest.fixture(scope="module")
def review_steps(workflow: Workflow) -> list[WorkflowStep]:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    review = jobs.get("review")
    assert isinstance(review, dict), "expected a 'review' job"
    steps = review.get("steps", [])
    assert isinstance(steps, list)
    return [cast(WorkflowStep, step) for step in steps if isinstance(step, dict)]


def _step_named(steps: list[WorkflowStep], name: str) -> WorkflowStep:
    step = next((item for item in steps if item.get("name") == name), None)
    assert step is not None, f"missing workflow step: {name}"
    return step


def test_workflow_file_exists() -> None:
    assert WORKFLOW.exists(), "Bernstein PR review workflow must exist"


def test_pr_review_runs_local_action_from_base_checkout(review_steps: list[WorkflowStep]) -> None:
    """The local action must not execute PR head code while the API key is set."""
    review_step = _step_named(review_steps, "Review PR")
    review_index = review_steps.index(review_step)

    checkout_steps = [
        step
        for step in review_steps[:review_index]
        if isinstance(step.get("uses"), str) and str(step["uses"]).startswith("actions/checkout@")
    ]
    assert checkout_steps, "Review PR must be preceded by a checkout of trusted action code"
    trusted_checkout = checkout_steps[-1]
    checkout_with = trusted_checkout.get("with", {})
    assert isinstance(checkout_with, dict)
    assert checkout_with.get("ref") == "${{ github.event.pull_request.base.sha }}", (
        "Review PR must run `uses: ./` from the base checkout, not from pull_request.head.sha"
    )
    assert checkout_with.get("persist-credentials") is False

    for step in checkout_steps:
        step_with = step.get("with", {})
        assert isinstance(step_with, dict)
        assert step_with.get("ref") != "${{ github.event.pull_request.head.sha }}", (
            "PR head code must not be checked out before running the local action with ANTHROPIC_API_KEY"
        )

    fetch_diff = _step_named(review_steps, "Fetch PR diff")
    assert review_steps.index(fetch_diff) < review_index
    run = fetch_diff.get("run", "")
    assert isinstance(run, str)
    assert ".bernstein-pr.diff" in run, "PR diff must be fetched as data for review context"
    assert "github.event.pull_request.diff_url" not in run, "PR diff URL expression must not be expanded in shell"
    assert "${PR_DIFF_URL}" in run
    fetch_env = fetch_diff.get("env", {})
    assert isinstance(fetch_env, dict)
    assert fetch_env.get("PR_DIFF_URL") == "${{ github.event.pull_request.diff_url }}"

    assert review_step.get("uses") == "./"
    inputs = review_step.get("with", {})
    assert isinstance(inputs, dict)
    task = inputs.get("task", "")
    assert isinstance(task, str)
    assert ".bernstein-pr.diff" in task, "Review task must point the action at the fetched PR diff"


def test_label_gated_review_also_triggers_on_labeled(workflow: Workflow) -> None:
    """A label that gates the job must also be able to start it.

    The ``review`` job runs only when the ``deep-review`` label is present. That
    label is usually added by a maintainer after the PR is already open and
    ready for review. Without ``labeled`` in the trigger list, adding it does
    nothing until the contributor happens to push again -- the label reads as a
    switch that is not wired to anything.
    """
    triggers = cast(dict[str, object], workflow.get("on") or workflow.get(True))
    pull_request = triggers.get("pull_request")
    assert isinstance(pull_request, dict), "expected a pull_request trigger"
    types = pull_request.get("types")
    assert isinstance(types, list)

    review = cast(dict[str, object], cast(dict[str, object], workflow["jobs"])["review"])
    condition = str(review.get("if", ""))
    assert "deep-review" in condition, "this test assumes the job is label-gated"

    assert "labeled" in types, (
        f"the review job is gated on the 'deep-review' label, so 'labeled' must be a trigger type; got {types}"
    )


def test_skipped_review_is_distinguishable_at_the_merge_decision(workflow: Workflow) -> None:
    """A run that reviewed nothing must not present as a run that found nothing.

    GitHub withholds secrets from ``pull_request`` runs on fork branches, so the
    review cannot run for any external contribution (#3601). What must not
    happen is that the resulting check looks identical to a completed review:
    the check name is the only thing a reviewer reads before merging, so it is
    the surface that has to carry the outcome.
    """
    review = cast(dict[str, object], cast(dict[str, object], workflow["jobs"])["review"])
    name = str(review.get("name", ""))

    assert "github.event.pull_request.head.repo.fork" in name, (
        "the check name must branch on whether the PR head lives in a fork, "
        f"otherwise a skipped review is indistinguishable from a completed one; got {name!r}"
    )
    assert "did not run" in name, f"the fork-PR check name must state that no review ran; got {name!r}"


def test_skip_reason_names_the_cause_and_fails_a_same_repo_misconfiguration(
    review_steps: list[WorkflowStep],
) -> None:
    """The two skip causes are different failures and must not share an outcome.

    A fork PR is a platform rule -- nothing is wrong with the repository, so the
    job reports the fact and succeeds. An empty key on a same-repository PR is a
    missing secret, which is the maintainer's to fix, so it fails instead of
    reporting a green check that no one can tell apart from a real review.
    """
    check = _step_named(review_steps, "Check API key")
    env = check.get("env", {})
    assert isinstance(env, dict)
    assert env.get("IS_FORK") == "${{ github.event.pull_request.head.repo.fork }}", (
        "the check step needs the fork flag to tell the two causes apart"
    )

    run = check.get("run", "")
    assert isinstance(run, str)

    assert "fork" in run.lower(), "the skip reason must name the cause, not only the symptom"
    assert "ANTHROPIC_API_KEY" in run
    assert "GITHUB_STEP_SUMMARY" in run, "the fork skip must be recorded where a reader can find it"
    assert "::warning" in run, "a skipped review must annotate the run, not pass silently"

    lines = [line.strip() for line in run.strip().splitlines() if line.strip()]
    assert lines[-1] == "exit 1", (
        "an empty ANTHROPIC_API_KEY on a same-repository PR is a missing secret and must fail "
        f"the job, so the check step has to end on a non-zero exit; it ends on {lines[-1]!r}"
    )

    fork_guard = [index for index, line in enumerate(lines) if line.startswith('if [ "$IS_FORK"')]
    assert fork_guard, "expected an explicit fork branch in the check step"
    assert "exit 0" in lines[fork_guard[0] :], "a fork PR must not be reddened for a platform rule"
