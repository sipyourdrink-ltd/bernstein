"""What ``bernstein demo --flask-todo`` reports when the run did not work.

The scenario printed three red crosses, then `Tasks completed 0 / 0`, then
`Completed 0 tasks`, and exited 0 (issue #3902). Three separate claims, none of
them true, and the exit code is the one a CI job reads.

The standard ``bernstein demo`` path already got this right in #2799: count
against the seeded set, snapshot before teardown, exit nonzero on any failure.
That fix landed on one of the two demo paths. These tests hold the other one to
the same contract, so the next change cannot quietly let them diverge again.
"""

from __future__ import annotations

import io
from unittest.mock import patch

from click.testing import CliRunner
from rich.console import Console

from bernstein.cli import run_confirm
from bernstein.cli.commands import quickstart_cmd as qs
from bernstein.cli.main import cli

SEEDED = len(qs._QUICKSTART_TASKS)


def _outcome(done: int, *, reachable: bool = True) -> run_confirm._DemoOutcome:
    """Build the snapshot the command would have taken from a live server."""
    return run_confirm._DemoOutcome(
        done=done,
        failed=SEEDED - done,
        total=SEEDED,
        cost_usd=0.0,
        server_reachable=reachable,
    )


def _invoke(outcome, tmp_path, *, bootstrap_error: Exception | None = None):
    """Run ``demo --flask-todo`` with the heavy seams stubbed.

    Bootstrap, poll, teardown and the outcome fetch are all patched, so the run
    exercises only the command's own reporting: the finished banner, the summary
    and the exit code, against a controlled ``outcome``.
    """
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    runner = CliRunner()
    boot = patch(
        "bernstein.core.bootstrap.bootstrap_from_goal",
        **({"side_effect": bootstrap_error} if bootstrap_error else {}),
    )
    with (
        patch.object(qs, "_setup_quickstart_project"),
        patch.object(qs, "_poll_until_done"),
        patch.object(qs, "_stop_quickstart_processes"),
        patch.object(qs, "_fetch_demo_outcome", return_value=outcome),
        patch("tempfile.mkdtemp", return_value=str(project_dir)),
        boot,
    ):
        return runner.invoke(cli, ["demo", "--flask-todo", "--timeout", "1", "--keep"])


def _render_summary(outcome, project_dir) -> str:
    """Render the summary table alone, with no command around it."""
    buf = io.StringIO()
    with patch.object(qs, "console", Console(file=buf, width=120, force_terminal=False)):
        qs._print_quickstart_summary(project_dir, outcome, elapsed_secs=120.0, keep=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Exit code - a wrapper script has to be able to tell the runs apart
# ---------------------------------------------------------------------------


def test_a_run_in_which_every_seeded_task_failed_exits_non_zero(tmp_path) -> None:
    """The headline defect: three failures, exit 0, nothing prompts a look."""
    result = _invoke(_outcome(done=0), tmp_path)
    assert result.exit_code != 0, result.output


def test_a_partially_completed_run_exits_non_zero(tmp_path) -> None:
    """Fewer done than seeded is a failed demo, not a qualified success."""
    result = _invoke(_outcome(done=SEEDED - 1), tmp_path)
    assert result.exit_code != 0, result.output


def test_a_run_in_which_every_seeded_task_finished_exits_zero(tmp_path) -> None:
    """The other half of the contract: a good run must stay green.

    Without this, "always exit 1" would pass every other test in this file.
    """
    result = _invoke(_outcome(done=SEEDED), tmp_path)
    assert result.exit_code == 0, result.output


def test_a_crashed_bootstrap_exits_non_zero(tmp_path) -> None:
    """The bootstrap error was printed and then fallen through to exit 0."""
    result = _invoke(None, tmp_path, bootstrap_error=RuntimeError("boom"))
    assert result.exit_code != 0, result.output


def test_the_finished_banner_does_not_announce_success_for_a_failed_run(tmp_path) -> None:
    """`✓ Orchestration finished` printed directly under three red crosses."""
    result = _invoke(_outcome(done=0), tmp_path)
    assert "unresolved tasks" in result.output, result.output


# ---------------------------------------------------------------------------
# Denominator - the summary and the progress line count the same set
# ---------------------------------------------------------------------------


def test_the_denominator_is_the_seeded_count_not_the_live_task_list(tmp_path) -> None:
    """`0 / 0` reads as "nothing was asked of it" - the opposite of what happened.

    The seeded count is what the progress line counts against and what
    ``docs/getting-started/quickstart-demo.md`` promises, so it is the only
    denominator that can agree with both.
    """
    out = _render_summary(_outcome(done=0), tmp_path)

    assert f"/ {SEEDED}" in out
    assert "/ 0" not in out


def test_the_seeded_count_is_the_three_tasks_the_docs_promise() -> None:
    """The denominator is only right if the constant it reads is right."""
    assert SEEDED == 3


def test_the_summary_counts_against_the_same_total_the_poll_waits_for(tmp_path) -> None:
    """One source of truth, not two values that happen to agree today.

    The poll is handed ``expected_total`` and the summary reads ``total`` off the
    snapshot built with it, so a change to the seeded set moves both at once.
    """
    with (
        patch.object(qs, "_setup_quickstart_project"),
        patch.object(qs, "_stop_quickstart_processes"),
        patch.object(qs, "_poll_until_done") as poll,
        patch.object(qs, "_fetch_demo_outcome", return_value=_outcome(done=SEEDED)) as fetch,
        patch("bernstein.core.bootstrap.bootstrap_from_goal"),
        patch("tempfile.mkdtemp", return_value=str(tmp_path)),
    ):
        CliRunner().invoke(cli, ["demo", "--flask-todo", "--timeout", "1", "--keep"])

    assert poll.call_args.kwargs["expected_total"] == SEEDED
    assert fetch.call_args.kwargs["expected_total"] == SEEDED


# ---------------------------------------------------------------------------
# Ordering - the snapshot is worthless once the server is gone
# ---------------------------------------------------------------------------


def test_the_snapshot_is_taken_before_the_server_is_stopped(tmp_path) -> None:
    """The root cause of `0 / 0`, and it did not need the server to crash.

    The summary queried ``/status`` itself, from *after* the cleanup that kills
    the task server. Every run read an empty list, so every run rendered zeros -
    the report's crash only made the race deterministic.
    """
    calls: list[str] = []

    with (
        patch.object(qs, "_setup_quickstart_project"),
        patch.object(qs, "_poll_until_done"),
        patch.object(qs, "_stop_quickstart_processes", side_effect=lambda *_: calls.append("stop")),
        patch.object(
            qs,
            "_fetch_demo_outcome",
            side_effect=lambda *_a, **_k: (calls.append("fetch"), _outcome(done=SEEDED))[1],
        ),
        patch("bernstein.core.bootstrap.bootstrap_from_goal"),
        patch("tempfile.mkdtemp", return_value=str(tmp_path)),
    ):
        CliRunner().invoke(cli, ["demo", "--flask-todo", "--timeout", "1", "--keep"])

    assert calls == ["fetch", "stop"], calls


def test_an_unreachable_server_is_named_rather_than_rendered_as_zeros(tmp_path) -> None:
    """A dead server and a wholly failed run produce identical counts.

    Both read done=0, and they call for different reactions - one is "the agents
    could not do the work", the other is "these numbers are not the run's".
    """
    # A neutral directory name: the summary prints the project path, and
    # pytest names its tmp dir after the test, so `tmp_path` itself would
    # satisfy a substring check on "unreachable" without the row existing.
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    out = _render_summary(_outcome(done=0, reachable=False), project_dir)
    assert "Task server" in out
    assert "unreachable" in out

    healthy = _render_summary(_outcome(done=0, reachable=True), project_dir)
    assert "Task server" not in healthy
