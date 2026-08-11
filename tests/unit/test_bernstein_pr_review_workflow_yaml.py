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
