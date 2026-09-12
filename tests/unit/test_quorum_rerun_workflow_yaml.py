"""The hourly sweep has to be able to find the runs it exists to re-run.

`quorum-rerun.yml` is the only thing that closes the 72-hour objection window: the requirement is
satisfied by the clock rather than by an event, so without the sweep a governance pull request stays
red until somebody pushes -- and a push restarts the window.

It looked its runs up with ``gh run list --workflow quorum.yml``. The repository carries two workflow
registrations for that file: the one ``/actions/workflows`` lists, whose only runs are
``workflow_dispatch``, and the one every ``pull_request`` and ``merge_group`` run of the check
actually carries, which 404s. So the lookup returned nothing for exactly the runs the sweep exists to
re-run, and every governance pull request was reported as "already green or never ran" while its
check sat red (#5827).

Nothing about that failure is visible: the sweep is green, its log is a list of reassuring lines, and
the only symptom is a pull request that never goes green on its own. So the shape of the lookup is
asserted here rather than left to be noticed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "quorum-rerun.yml"


def _rerun_step() -> str:
    """The `run:` body of the step that finds and re-runs the check, comments included."""
    doc = cast("dict[str, Any]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    for step in doc["jobs"]["rerun"]["steps"]:
        if isinstance(step, dict) and step.get("name") == "Re-run quorum":
            run = step.get("run")
            assert isinstance(run, str)
            return run
    pytest.fail("quorum-rerun.yml has no step named 'Re-run quorum'")


def _rerun_code() -> str:
    """The same body with comments stripped.

    The file explains in prose why the old lookup was wrong, and that explanation has to be allowed
    to name the thing it removed -- otherwise the only way to pass these guards is to delete the
    reasoning, which is the opposite of what they are for.
    """
    return "\n".join(line for line in _rerun_step().splitlines() if not line.lstrip().startswith("#"))


def test_workflow_exists() -> None:
    assert WORKFLOW.is_file()


def test_the_run_is_not_looked_up_by_workflow_file() -> None:
    """`--workflow quorum.yml` resolves to the registration these runs are not on."""
    assert "--workflow quorum.yml" not in _rerun_code(), (
        "the sweep resolves quorum.yml, whose only runs are workflow_dispatch; every "
        "pull_request and merge_group run of the check carries a different, un-resolvable "
        "workflow id, so this lookup finds nothing for exactly the runs it is meant to find"
    )


def test_the_run_is_looked_up_by_commit() -> None:
    """The check run on the head commit is reachable without resolving any workflow."""
    step = _rerun_step()

    assert "check-runs?check_name=quorum" in step
    assert "${head}" in step or '"$head"' in step


def test_a_still_running_check_is_not_treated_as_a_red_one() -> None:
    """A queued or in-progress check has `conclusion: null`, which is not a failure.

    Re-running a check that has not finished cancels the answer that was coming, and the sweep
    runs every hour -- so the same pull request would be restarted forever and never conclude.
    """
    assert "conclusion != null" in _rerun_step()


def test_the_rerun_does_not_resolve_the_workflow_either() -> None:
    """`gh run rerun` refuses these runs for the same reason the lookup missed them.

    It resolves the workflow before posting. The REST endpoint does not, and works on them today.
    """
    step = _rerun_code()

    assert "gh run rerun" not in step
    assert "/rerun" in step
    assert "-X POST" in step


def test_the_sweep_logs_the_run_id_it_found() -> None:
    """The failure mode was a reassuring log, so the log has to carry the evidence.

    A run id in the line is what makes "it found nothing again" distinguishable from "there was
    nothing to find" without opening the API.
    """
    step = _rerun_step()

    assert "re-running quorum for #$pr (run $run" in step


def test_the_quiet_line_does_not_claim_more_than_it_knows() -> None:
    """ "Already green or never ran" was wrong in a third way: still running."""
    assert "already green, still running, or never ran" in _rerun_step()
