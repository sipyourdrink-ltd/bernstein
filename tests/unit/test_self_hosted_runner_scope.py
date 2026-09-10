"""Self-hosted runner jobs run only from ``main``.

A self-hosted runner executes whatever the job hands it, on a machine the
project owns. Code from a pull request must never reach one: the only
triggers a job may combine with a self-hosted ``runs-on`` are ``schedule``,
``workflow_dispatch`` and ``push`` restricted to ``main``. Anything else --
``pull_request``, ``pull_request_target``, ``merge_group``, ``workflow_run``,
``workflow_call``, ``issue_comment`` -- can carry a contributor's revision and
is refused here, so the mistake is caught in review rather than on the box.

Every workflow moved onto the project's own runner is covered here, so a
later edit that widens its triggers reddens this test instead of silently
exposing the box.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


WORKFLOWS = Path(".github/workflows")
ALLOWED_TRIGGERS = {"schedule", "workflow_dispatch", "push"}
SELF_HOSTED_MARKERS = ("self-hosted",)


def _runs_on_is_self_hosted(runs_on: object) -> bool:
    if isinstance(runs_on, str):
        return any(marker in runs_on for marker in SELF_HOSTED_MARKERS)
    if isinstance(runs_on, list):
        return any(_runs_on_is_self_hosted(item) for item in runs_on)
    # The mapping form (``group:`` / ``labels:``) only exists for runner
    # groups, which are self-hosted by definition.
    return isinstance(runs_on, dict)


def _matrix_names_self_hosted(job: dict[str, Any]) -> bool:
    strategy = job.get("strategy")
    if not isinstance(strategy, dict):
        return False
    return "self-hosted" in yaml.safe_dump(strategy.get("matrix"))


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key ``on`` as the boolean ``True``.
    raw = workflow.get(True, workflow.get("on"))
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(item): None for item in raw}
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    return {}


def _push_is_main_only(push: object) -> bool:
    if not isinstance(push, dict):
        return False  # a bare ``push`` fires for every branch and tag
    if push.get("branches") != ["main"]:
        return False
    return not any(key in push for key in ("tags", "branches-ignore", "tags-ignore"))


def violations(text: str, name: str = "<workflow>") -> list[str]:
    """Return one line per job that pairs a self-hosted runner with a trigger
    that can carry pull-request code."""
    workflow = yaml.safe_load(text)
    if not isinstance(workflow, dict):
        return []
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return []
    triggers = _triggers(workflow)
    found: list[str] = []
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if not (_runs_on_is_self_hosted(job.get("runs-on")) or _matrix_names_self_hosted(job)):
            continue
        bad = sorted(set(triggers) - ALLOWED_TRIGGERS)
        if "push" in triggers and not _push_is_main_only(triggers["push"]):
            bad.append("push (not restricted to branches: [main])")
        if bad:
            found.append(f"{name}: job '{job_name}' uses a self-hosted runner with {', '.join(bad)}")
    return found


def test_self_hosted_jobs_only_run_from_main() -> None:
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert files, "no workflows found; run from the repository root"
    found = [line for path in files for line in violations(path.read_text(encoding="utf-8"), path.name)]
    assert not found, "\n".join(found)


REJECTED = """
on:
  pull_request:
  workflow_dispatch:
jobs:
  build:
    runs-on: [self-hosted, linux]
    steps: [{run: "true"}]
"""

REJECTED_BARE_PUSH = """
on: push
jobs:
  build:
    runs-on:
      group: project-runners
    steps: [{run: "true"}]
"""

ACCEPTED = """
on:
  schedule: [{cron: "0 3 * * *"}]
  workflow_dispatch:
  push:
    branches: [main]
jobs:
  sweep:
    runs-on: [self-hosted, linux]
    steps: [{run: "true"}]
  hosted:
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
"""


def test_checker_refuses_pull_request_triggers() -> None:
    assert violations(REJECTED, "x.yml") == ["x.yml: job 'build' uses a self-hosted runner with pull_request"]


def test_checker_refuses_unrestricted_push() -> None:
    (line,) = violations(REJECTED_BARE_PUSH, "x.yml")
    assert "push (not restricted to branches: [main])" in line


def test_checker_accepts_main_only_triggers() -> None:
    assert violations(ACCEPTED, "x.yml") == []
