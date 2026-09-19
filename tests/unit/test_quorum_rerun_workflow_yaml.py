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

The sweep had a second blind spot with the same symptom. `pull_request_review` recomputes the check
only since 2026-09-12 (#5858), so every approval given before that trigger existed sits in a check
run nobody recomputed -- and the governance pass reaches only pull requests touching a governance
file. #5713 stayed BLOCKED for two days with its quorum satisfied and a green run already on its
head. The sweep now also re-runs a quorum run that is older than the newest review on its pull
request, which is what that staleness looks like from the API; those guards are below too.
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


def test_a_stale_verdict_is_found_by_comparing_a_review_to_the_run() -> None:
    """An approval older than the `pull_request_review` trigger recomputes nothing on its own.

    The governance pass covers only pull requests touching a governance file, so every other
    pull request kept whatever verdict it was last given. What marks one is that a review is
    newer than a quorum run still red on the head; the sweep has to read both timestamps.
    """
    step = _rerun_step()

    assert "submittedAt" in step, "the sweep needs each review's timestamp to spot a stale verdict"
    assert ".started_at" in step, "the run's own timestamp is the other half of the comparison"
    assert "$before" in _rerun_code(), "the comparison has to reach the run filter"


def test_a_pull_request_with_no_review_is_not_looked_at() -> None:
    """Nothing to be stale against, and the lookup costs two API calls per pull request."""
    assert "select(.reviews | length > 0)" in _rerun_step()


def test_every_red_run_is_re_run_not_only_the_newest() -> None:
    """Branch protection folds every check run of a required name into one verdict.

    One leftover red holds the pull request at BLOCKED however many later runs concluded
    success (#3042, #3154), so taking the first match left the pull request exactly as stuck.
    """
    code = _rerun_code()

    assert "| head -1" not in code, (
        "a `head -1` re-runs one red instance and leaves the rest; the fold means the pull "
        "request stays BLOCKED on any one of them"
    )
    assert "while read -r started_at run" in code, "the sweep iterates the red runs"


def test_the_sweep_has_a_re_run_budget() -> None:
    """The pool ceiling is 20 concurrent jobs and the merge queue needs most of it.

    A backlog of stale verdicts would otherwise start every re-run at once, on the hour, and
    the thing being unblocked is the same queue that loses the runners.
    """
    code = _rerun_code()

    assert "MAX_RERUNS" in code
    assert 'started" -ge "$MAX_RERUNS' in code


def test_the_budget_is_checked_before_the_lookups() -> None:
    """An exhausted sweep must not spend two API calls per remaining pull request.

    The check sits at the top of the function, before `gh pr view`, so a deferred pull request
    costs a log line and nothing else.
    """
    code = _rerun_code()
    body = code[code.index("rerun_for () {") :]
    budget = body.index('"$started" -ge "$MAX_RERUNS"')
    lookup = body.index("gh pr view")

    assert budget < lookup, "the budget is checked after the lookup it is supposed to save"


def test_a_governance_pull_request_is_not_swept_twice() -> None:
    """Pass 1 already re-ran it; pass 2 would re-run the attempt pass 1 just started."""
    assert "$handled" in _rerun_code()


def test_the_quiet_line_distinguishes_current_from_absent() -> None:
    """ "Already green, still running, or never ran" is wrong for a red run that is current.

    A quorum run newer than every review is giving the right answer, and saying it is green
    would hide a pull request that is genuinely failing the gate.
    """
    step = _rerun_step()

    assert "quorum last ran after the newest review; its verdict is current" in step
    assert "already green, still running, or never ran" in step
