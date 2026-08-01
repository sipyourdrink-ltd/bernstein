"""Structural assertions for the docs-drift removed-verb gate.

The workflow greps the docs tree for command surfaces that no longer exist in
the CLI. The gate only works while every removed verb is still listed and the
step stays unconditional - an ``if:`` on the step would silently reduce it to
the advisory behaviour of the drift report it sits next to.

These tests pin both properties, plus the fact that the guarded verbs really
are absent from the CLI, so the list cannot rot into checking for commands
that were reinstated.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast

import pytest
import yaml


class WorkflowStep(TypedDict, total=False):
    name: object
    run: object
    if_: object


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-drift.yml"

STEP_NAME = "Fail on docs referencing removed CLI verbs"

#: Verb -> the ``bernstein`` group it was removed from. Every entry must be
#: absent from that group's command list and present in the workflow grep.
REMOVED_VERBS: dict[str, str] = {
    "rate-limit": "chaos",
    "agent-oom": "chaos",
    "disk-full": "chaos",
    "deploy": "cloud",
}


def _steps() -> list[dict[str, object]]:
    data = cast("dict[str, object]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    jobs = data.get("jobs")
    assert isinstance(jobs, dict), "docs-drift.yml has no jobs mapping"
    job = jobs.get("drift-check")
    assert isinstance(job, dict), "expected a drift-check job"
    steps = job.get("steps")
    assert isinstance(steps, list), "drift-check job has no steps"
    return [step for step in steps if isinstance(step, dict)]


def _removed_verb_step() -> dict[str, object]:
    for step in _steps():
        if step.get("name") == STEP_NAME:
            return step
    pytest.fail(f"docs-drift.yml has no {STEP_NAME!r} step")


def test_removed_verb_step_exists_and_is_unconditional() -> None:
    """The gate must run on every event, including pull requests."""
    step = _removed_verb_step()
    assert "if" not in step, f"{STEP_NAME!r} must not be conditional - it gates pull requests too"
    assert isinstance(step.get("run"), str)


@pytest.mark.parametrize(("verb", "group"), sorted(REMOVED_VERBS.items()))
def test_workflow_greps_for_each_removed_verb(verb: str, group: str) -> None:
    """Every removed verb is named in the workflow's grep patterns."""
    script = cast("str", _removed_verb_step()["run"])
    assert f"bernstein {group} [a-z/-]*{verb}" in script, f"no grep pattern guards `bernstein {group} {verb}`"


@pytest.mark.parametrize(("verb", "group"), sorted(REMOVED_VERBS.items()))
def test_guarded_verbs_are_absent_from_the_cli(verb: str, group: str) -> None:
    """A guarded verb must not exist on the CLI group it was removed from."""
    from bernstein.cli.main import cli

    group_cmd = cli.commands.get(group)
    assert group_cmd is not None, f"expected a `bernstein {group}` group"
    subcommands = getattr(group_cmd, "commands", {})
    assert verb not in subcommands, f"`bernstein {group} {verb}` exists again; drop it from the removed-verb gate"


def test_removed_test_chaos_flag_is_guarded_and_gone() -> None:
    """The `bernstein test --chaos` flag is both guarded and absent."""
    from bernstein.cli.main import cli

    script = cast("str", _removed_verb_step()["run"])
    assert "--chaos" in script, "no grep pattern guards the removed `--chaos` flag"

    test_cmd = cli.commands.get("test")
    assert test_cmd is not None, "expected a `bernstein test` command"
    flags = {opt for param in test_cmd.params for opt in getattr(param, "opts", [])}
    assert "--chaos" not in flags, "`bernstein test --chaos` exists again; drop it from the removed-verb gate"
