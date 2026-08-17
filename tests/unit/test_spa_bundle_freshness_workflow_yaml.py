"""Structural assertions for the SPA bundle freshness gate.

The gate rebuilds ``web/`` and fails when the result differs from the bundle
committed under ``src/bernstein/gui/static``. It is the only thing that
notices a dependency bump which leaves the shipped UI behind, so the parts
that make it work are pinned here rather than left to review:

* it triggers on the paths that can move the bundle - a trigger that misses
  ``web/**`` turns the gate off for exactly the change it exists to catch;
* it compares with ``--untracked-files=all``, because ``emptyOutDir`` writes
  the replacement asset under a new content hash and a plain ``git diff``
  would not see an untracked file;
* the comparison step is unconditional, since an ``if:`` would reduce it to
  advisory;
* it declares a cancelling per-PR concurrency group, the rule
  ``tests/unit/test_pull_request_workflow_concurrency_yaml.py`` holds for
  every ``pull_request`` workflow;
* no job is named ``CI gate``, which the required-check canary allow-lists
  to exactly two workflows;
* it triggers on ``merge_group`` with no filter, so it reports on a queued
  ref and branch protection can require it. Being required is a separate,
  later, maintainer-only change; this file pins the half that has to come
  first, because the reverse order wedges the queue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "spa-bundle-freshness.yml"

JOB_KEY = "rebuild"
COMPARE_STEP = "Compare against what is committed"
BUNDLE_DIR = "src/bernstein/gui/static"

#: Every path whose change can produce a different bundle.
REQUIRED_TRIGGER_PATHS = ("web/**", f"{BUNDLE_DIR}/**")


def _doc() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def _on() -> dict[str, Any]:
    # PyYAML 1.1 parses a bare ``on:`` key as the boolean True.
    doc = _doc()
    on = doc.get(True, doc.get("on"))
    assert isinstance(on, dict), "workflow must declare a mapping of triggers"
    return cast("dict[str, Any]", on)


def _job() -> dict[str, Any]:
    jobs = _doc().get("jobs")
    assert isinstance(jobs, dict), f"{WORKFLOW.name} has no jobs mapping"
    job = jobs.get(JOB_KEY)
    assert isinstance(job, dict), f"expected a {JOB_KEY!r} job"
    return cast("dict[str, Any]", job)


def _steps() -> list[dict[str, Any]]:
    steps = _job().get("steps")
    assert isinstance(steps, list), f"{JOB_KEY!r} job has no steps"
    return [step for step in steps if isinstance(step, dict)]


def _compare_step() -> dict[str, Any]:
    for step in _steps():
        if step.get("name") == COMPARE_STEP:
            return step
    pytest.fail(f"{WORKFLOW.name} has no {COMPARE_STEP!r} step")


def test_workflow_exists() -> None:
    assert WORKFLOW.is_file(), f"{WORKFLOW} is missing"


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("path", REQUIRED_TRIGGER_PATHS)
def test_trigger_covers_every_path_that_moves_the_bundle(event: str, path: str) -> None:
    """A trigger that misses these paths silently disables the gate."""
    trigger = _on().get(event)
    assert isinstance(trigger, dict), f"{event} trigger must be a mapping"
    paths = trigger.get("paths")
    assert isinstance(paths, list), f"{event} trigger must filter on paths"
    assert path in paths, f"{event} trigger does not cover {path!r}"


def test_pull_request_runs_cancel_superseded_ones() -> None:
    """Matches the repo-wide rule for ``pull_request`` workflows."""
    concurrency = _doc().get("concurrency")
    assert isinstance(concurrency, dict), "workflow must declare a concurrency group"
    assert concurrency.get("cancel-in-progress") is True
    assert "github.event.pull_request.number" in str(concurrency.get("group", ""))


def test_comparison_is_unconditional() -> None:
    """An ``if:`` here would quietly turn the gate advisory."""
    step = _compare_step()
    assert "if" not in step, f"{COMPARE_STEP!r} must not be conditional"
    assert isinstance(step.get("run"), str)


def test_comparison_sees_untracked_replacement_assets() -> None:
    """``emptyOutDir`` renames the asset, so the new one arrives untracked."""
    run = str(_compare_step().get("run", ""))
    assert "--untracked-files=all" in run, "an untracked replacement asset would be missed"
    assert BUNDLE_DIR in run, f"the comparison must be scoped to {BUNDLE_DIR}"
    assert "exit 1" in run, "the step must fail the job on drift"


def test_workflow_installs_from_the_lockfile_before_rebuilding() -> None:
    """``npm ci`` (not ``npm install``) - the point is the pinned tree.

    Presence alone is not the property: a build that runs before the install,
    or either step running outside ``web/``, rebuilds against something other
    than the lockfile and the gate compares the wrong bundle. So the order
    and the working directory are asserted, not just that both steps exist.
    """
    steps = _steps()
    install = [i for i, step in enumerate(steps) if str(step.get("run", "")).strip() == "npm ci"]
    build = [i for i, step in enumerate(steps) if "npm run build" in str(step.get("run", ""))]

    assert install, "the gate must install from the lockfile"
    assert build, "the gate must rebuild the bundle"
    assert install[0] < build[0], (
        "`npm ci` must run before `npm run build`, otherwise the rebuild uses "
        "whatever tree is already on the runner rather than the pinned one"
    )

    for index in (install[0], build[0]):
        step = steps[index]
        assert step.get("working-directory") == "web", (
            f"step {step.get('name', step.get('run'))!r} must run in `web/`, the only directory the lockfile pins"
        )


def test_no_job_emits_the_required_check_name() -> None:
    """The canary allow-lists exactly two emitters of the ``CI gate`` check.

    A check run takes the job's ``name:``, so that is what has to stay clear
    of the required-context string - prose about it in a comment is fine.
    """
    jobs = _doc().get("jobs")
    assert isinstance(jobs, dict)
    names = [body.get("name") for body in jobs.values() if isinstance(body, dict)]
    assert "CI gate" not in names, f"{WORKFLOW.name} would post a third `CI gate` check"


def test_merge_group_trigger_present() -> None:
    """The gate must report on a queued ref before it can be required.

    Until #4028 this file asserted the opposite - that no ``merge_group``
    trigger existed - and its docstring said to replace it with this once
    the trigger was added. #4010 is why: the lane went red on the pull
    request, ``CI gate`` stayed green, and the queue merged it because an
    advisory lane gates nothing.

    Adding the trigger does not make the check required; that is a branch
    protection change, and it is deliberately the later half. This assertion
    pins the earlier half, which is the half that has to exist first.
    """
    assert "merge_group" in _on(), (
        "without a `merge_group` trigger this lane cannot report on a queued ref, so it cannot be a "
        "required context - and while it is not required, a red bundle merges (#4010)"
    )


def test_merge_group_trigger_carries_no_filter() -> None:
    """The queue runs it on every group, not only ``web/**`` ones.

    Two independent reasons, and either alone is sufficient. ``paths`` and
    ``paths-ignore`` are evaluated for ``push``, ``pull_request`` and
    ``pull_request_target`` only - never for ``merge_group`` - so a filter
    here is silently ignored and reads as a guarantee nobody is honouring.
    And a queue batch stacks several pull requests, so the batch can carry
    a ``web/**`` change even when the entry under test does not; filtering
    on the entry's own diff would let exactly this defect back in.

    Mirrors ``test_ci_merge_group_trigger_carries_no_filter``.
    """
    trigger = _on().get("merge_group", "__absent__")
    assert trigger != "__absent__", "no `merge_group` trigger at all"
    assert trigger in (None, {}), (
        f"the `merge_group` trigger carries a filter ({trigger!r}). GitHub does not evaluate `paths` for "
        "`merge_group`, and a batch can contain a `web/**` change the tested entry does not."
    )
