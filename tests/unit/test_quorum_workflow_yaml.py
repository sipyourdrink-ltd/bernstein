"""Structural assertions for the review-quorum check.

The `quorum` check is the only gate that answers the review question: the
branch ruleset on the default branch asks for zero native approvals, so a
green `quorum` is the whole approval signal a merge is allowed to rely on.
That makes two properties of the workflow file load-bearing, and neither one
shows up as a red check when it regresses:

*Stale rules.* The rules the check applies -- the script, the roster,
CODEOWNERS -- come from a checkout. If that checkout is the commit a pull
request records as its base, then a pull request opened before a rule existed
is measured against a tree that does not carry it, and the answer is a green
check for a state the current rules reject. A pull request that sits open long
enough decides for itself which rules it is judged by.

*Passing without applying a rule.* A run that finds no script has no rules to
apply. Reporting that as success makes "no rule was applied" and "the review
happened" the same check result, which is the failure the gate exists to
prevent; the same shape reappears whenever the missing-script branch is
allowed to exit zero.

Both are decided by static structure, so they are asserted statically here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

QUORUM = Path(".github/workflows/quorum.yml")
SCRIPT = Path("scripts/quorum_check.py")

#: The expression that has to decide the checkout. Anything derived from the
#: pull request's recorded base pins the rules to the moment it was opened.
DEFAULT_BRANCH_EXPR = "github.event.repository.default_branch"
STALE_BASE_EXPRS = (
    "github.event.pull_request.base.sha",
    "github.event.merge_group.base_sha",
)

#: The three files the check reads. A sparse checkout that drops one of them
#: reads a missing roster or a missing CODEOWNERS as "no rule here".
READS = (
    "scripts/quorum_check.py",
    ".github/quorum-roster.toml",
    ".github/CODEOWNERS",
)

SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def _load() -> dict[str, Any]:
    doc = yaml.safe_load(QUORUM.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{QUORUM} is not a mapping"
    return cast("dict[str, Any]", doc)


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML 1.1 parses a bare ``on:`` key as the boolean True.
    on = cast("dict[Any, Any]", doc).get(True, doc.get("on"))
    assert isinstance(on, dict), "the workflow must have an `on:` mapping"
    return cast("dict[str, Any]", on)


def _job(doc: dict[str, Any]) -> dict[str, Any]:
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict), "the workflow must define jobs"
    job = jobs.get("quorum")
    assert isinstance(job, dict), "the workflow must define the `quorum` job"
    return cast("dict[str, Any]", job)


def _steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    steps = _job(doc).get("steps")
    assert isinstance(steps, list), "the `quorum` job must define steps"
    return [step for step in steps if isinstance(step, dict)]


def _checkout(doc: dict[str, Any]) -> dict[str, Any]:
    matches = [step for step in _steps(doc) if str(step.get("uses", "")).startswith("actions/checkout@")]
    assert len(matches) == 1, "the job must check out exactly once"
    return matches[0]


def _check_step(doc: dict[str, Any]) -> dict[str, Any]:
    matches = [step for step in _steps(doc) if "run" in step]
    assert len(matches) == 1, "the job must have exactly one `run` step"
    return matches[0]


def test_the_rules_are_read_from_the_default_branch() -> None:
    """The checkout is the default branch, not the pull request's recorded base.

    A base commit older than a rule does not carry it, so measuring a pull
    request against its own recorded base lets an old pull request pass rules
    it never saw. The default branch is where the ruleset pins this workflow
    from, and it is where the rules in force live.
    """
    checkout = _checkout(_load())
    with_ = checkout.get("with")
    assert isinstance(with_, dict), "the checkout step must have a `with:` mapping"
    ref = with_.get("ref")
    assert isinstance(ref, str), "the checkout step must pin `ref`"

    assert DEFAULT_BRANCH_EXPR in ref, f"the checkout must read `{DEFAULT_BRANCH_EXPR}`; got {ref!r}"
    for expr in STALE_BASE_EXPRS:
        assert expr not in ref, f"the checkout must not fall back to `{expr}`: it pins the rules to the pull request"


def test_the_checkout_cannot_run_the_branch_under_review() -> None:
    """Read-only, credential-free, and limited to the three files it reads."""
    doc = _load()
    with_ = _checkout(doc).get("with")
    assert isinstance(with_, dict)

    assert with_.get("persist-credentials") is False, "the checkout must not leave a token behind"
    assert with_.get("sparse-checkout-cone-mode") is False

    sparse = with_.get("sparse-checkout")
    assert isinstance(sparse, str), "the checkout must stay sparse"
    listed = [line.strip() for line in sparse.splitlines() if line.strip()]
    assert listed == list(READS), f"the sparse checkout must be exactly the files the check reads; got {listed}"

    assert doc.get("permissions") == {"contents": "read"}
    assert _job(doc).get("permissions") == {"contents": "read", "pull-requests": "read"}


def test_a_run_that_applied_no_rule_fails() -> None:
    """The missing-script branch fails; nothing in the step can exit zero early.

    `exit 0` there is the shape that turns "no rule was applied" into a green
    review check, which is the one answer this gate must never give.
    """
    run = _check_step(_load()).get("run")
    assert isinstance(run, str), "the check step must have a `run:` body"

    assert "exit 1" in run, "the missing-script branch must fail the job"
    assert "exit 0" not in run, "the check step must not exit zero without applying a rule"
    assert f"[ ! -f {READS[0]} ]" in run, "the step must still guard on the script being present"
    assert "set -euo pipefail" in run


def test_the_check_still_answers_for_every_event_that_can_gate_a_merge() -> None:
    """Pull requests, queued groups, and a manual run against one pull request."""
    doc = _load()
    on = _on(doc)

    pull_request = on.get("pull_request")
    assert isinstance(pull_request, dict)
    types = pull_request.get("types")
    assert isinstance(types, list)
    assert {"opened", "synchronize", "reopened", "ready_for_review"} <= set(types)
    assert "paths" not in pull_request and "paths-ignore" not in pull_request, (
        "the review question is asked whatever the pull request touches"
    )

    assert "merge_group" in on, "the queued state has to be re-checked before it merges"

    dispatch = on.get("workflow_dispatch")
    assert isinstance(dispatch, dict)
    inputs = dispatch.get("inputs")
    assert isinstance(inputs, dict)
    assert "pr" in inputs, "a manual run has to name the pull request it answers for"


def test_every_action_stays_pinned_to_a_commit() -> None:
    """A tag is movable; the gate's own steps cannot be."""
    for step in _steps(_load()):
        uses = step.get("uses")
        if uses is None:
            continue
        assert isinstance(uses, str)
        assert SHA_PIN.match(uses), f"`{uses}` must be pinned to a full commit sha"


def test_the_file_records_why_the_rules_come_from_the_default_branch() -> None:
    """The reason is in the file, next to the line it explains.

    Both properties above were regressions somebody could introduce while
    reading the old comment and believing it, so the comment is asserted.
    """
    text = QUORUM.read_text(encoding="utf-8")
    assert "default branch" in text
    assert "organization ruleset" in text, "the file must keep naming what makes it non-forgeable"
    assert "BASE commit" not in text, "the comment must not still describe the recorded base commit"
    assert SCRIPT.exists(), "the script the gate runs must be on the default branch"
