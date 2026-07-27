"""Structural assertions for the nightly deep-test workflow.

``Nightly deep tests`` ran with ``continue-on-error: true`` on every job.
GitHub excludes such jobs from the run conclusion, so the run reported
``success`` while a job inside it reported ``failure``. A real Schemathesis
regression stayed invisible in the Actions list for eight consecutive
nights because of it.

There is no way to make a normal workflow job conclude ``neutral``: that
conclusion is only settable through the Checks API by a GitHub App. So the
run conclusion has to carry the signal, which means the job-level
``continue-on-error`` has to go.

Removing it is safe for this workflow specifically:

* it has no ``pull_request`` trigger, so it cannot block a PR;
* it is not a required status check, so it gates no merge;
* its jobs have no ``needs`` edges, so a failing job does not stop the
  others from running.

Genuinely advisory tool runs stay advisory at the step level, where the
scope of what is being tolerated is visible in the command itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nightly-deep-tests.yml"


def _load() -> dict[object, object]:
    # Keys are typed as ``object`` on purpose: PyYAML follows YAML 1.1, so
    # the bare ``on:`` trigger key loads as the boolean ``True`` rather
    # than the string ``"on"``.
    return cast("dict[object, object]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _jobs() -> dict[str, dict[str, object]]:
    jobs = _load().get("jobs")
    assert isinstance(jobs, dict), "nightly-deep-tests.yml must declare jobs"
    return cast("dict[str, dict[str, object]]", jobs)


def test_no_job_opts_out_of_the_run_conclusion() -> None:
    """A failed job must be able to fail the run.

    This is the regression guard: any job carrying ``continue-on-error``
    is silently excluded from the run conclusion, which is exactly how a
    red nightly reported ``success``.
    """
    offenders = [name for name, job in _jobs().items() if job.get("continue-on-error")]
    assert not offenders, (
        "these jobs opt out of the run conclusion, so the Actions list "
        f"cannot distinguish a clean night from a red one: {sorted(offenders)}"
    )


def test_workflow_does_not_gate_pull_requests() -> None:
    """The premise of the fix: this workflow blocks nothing.

    Removing ``continue-on-error`` is only safe while the workflow has no
    ``pull_request`` trigger. If someone adds one, the trade-off has to be
    re-decided rather than inherited.
    """
    document = _load()
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict), "expected a mapping of triggers"
    assert set(triggers) == {"schedule", "workflow_dispatch"}, (
        f"unexpected triggers {sorted(triggers)}: this workflow must stay off the PR path"
    )


def test_python_314_unit_lane_exists_here() -> None:
    """3.14 has exactly one lane that executes tests, and it is this one.

    ``ci.yml`` runs the unit matrix on 3.13 for pull requests and 3.12 + 3.13
    for pushes; ``ci-macos-nightly.yml`` is 3.13. Every other 3.14 pin in the
    repository installs the package without running a test. A regression that
    only reproduces on 3.14 is therefore invisible unless this job exists, and
    deleting it must be a deliberate act rather than a silent one.
    """
    job = _jobs().get("unit-python-314")
    assert job is not None, "the only unit lane on Python 3.14 was removed"

    steps = job.get("steps")
    assert isinstance(steps, list)
    scripts = [
        # Comments are stripped: prose about `--affected` is not a use of it.
        "\n".join(line for line in cast("str", step["run"]).splitlines() if not line.lstrip().startswith("#"))
        for step in cast("list[dict[str, object]]", steps)
        if isinstance(step.get("run"), str)
    ]
    body = "\n".join(scripts)

    assert "3.14" in yaml.safe_dump(job)
    assert "scripts/run_tests.py" in body, "the lane must execute the unit suite, not just install on 3.14"
    assert "--affected" not in body, (
        "this lane exists to see what the diff-narrowed pull_request lane cannot; "
        "narrowing it to a diff reintroduces the blind spot"
    )


def test_jobs_run_independently() -> None:
    """No ``needs`` edges, so one red job cannot skip the rest.

    This is what actually keeps the other probes reporting on a bad night,
    not ``continue-on-error``.
    """
    with_needs = [name for name, job in _jobs().items() if job.get("needs")]
    assert not with_needs, f"jobs must stay independent so a failure does not skip siblings: {sorted(with_needs)}"
