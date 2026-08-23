"""``bernstein pr --issue`` titles the PR from the issue and links it.

Without it the title comes from the run's goal, and the goal a fleet hands
an agent starts with the instructions it was given -- so an issue-driven run
opened PRs titled ``fix: resolve GitHub issue #4345: ...`` truncated
mid-word. The link was worse: nothing put a closing keyword anywhere GitHub
reads, so merging the PR left the issue open.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.integrations.pr_gen import CostBreakdown, SessionSummary
from bernstein.core.integrations.tickets import TicketParseError, TicketPayload

ISSUE = TicketPayload(
    id="sipyourdrink-ltd/bernstein#4345",
    title="Dead-agent orphan handling crashes the tick",
    description="body text",
    labels=("bug",),
    url="https://github.com/sipyourdrink-ltd/bernstein/issues/4345",
    source="github",
)

GOAL = (
    "Resolve GitHub issue #4345: Dead-agent orphan handling crashes the tick\n\n"
    "Work only inside this repository. Follow existing code style."
)


def _summary() -> SessionSummary:
    return SessionSummary(
        session_id="backend-1",
        goal=GOAL,
        branch="run-1",
        base_branch="main",
        primary_role="backend",
        diff_stat=" src/x.py | 2 +-",
        gates=(),
        cost=CostBreakdown(total_usd=0.0, total_tokens=0, by_role={}),
        evidence=None,
    )


def _run(args: list[str], *, ticket: Any = ISSUE) -> Any:
    """Invoke `bernstein pr --dry-run` with the session and issue stubbed."""
    fetch = MagicMock(side_effect=ticket) if isinstance(ticket, Exception) else MagicMock(return_value=ticket)
    slug = MagicMock(stdout="sipyourdrink-ltd/bernstein\n")
    with (
        patch("bernstein.cli.commands.pr_cmd.load_session_summary", return_value=_summary()),
        patch("bernstein.cli.commands.pr_cmd._enrich_summary_with_git", side_effect=lambda s, _w: s),
        patch("bernstein.cli.commands.pr_cmd.fetch_ticket", fetch),
        patch("bernstein.cli.commands.pr_cmd.shutil.which", return_value="/usr/bin/gh"),
        patch("bernstein.cli.commands.pr_cmd.subprocess.run", return_value=slug),
    ):
        result = CliRunner().invoke(cli, ["pr", "--dry-run", *args])
    result.fetch = fetch  # type: ignore[attr-defined]
    return result


def test_issue_is_a_registered_pr_option() -> None:
    from bernstein.cli.commands.pr_cmd import pr_cmd

    assert "--issue" in {p.opts[0] for p in pr_cmd.params}


def test_body_opens_with_the_closing_keyword() -> None:
    """A comment does not close an issue; only the body or a commit does."""
    out = _run(["--issue", "4345"]).output

    assert out.splitlines()[2].strip() == "Closes #4345"


def test_title_comes_from_the_issue_not_the_goal_preamble() -> None:
    """The regression this flag exists for."""
    out = _run(["--issue", "4345"]).output
    title = next(line for line in out.splitlines() if line.startswith("Title:"))

    assert "dead-agent orphan handling crashes the tick" in title.lower()
    assert "resolve github issue" not in title.lower()
    assert "…" not in title


@pytest.mark.parametrize("ref", ["4345", "#4345", "https://github.com/sipyourdrink-ltd/bernstein/issues/4345"])
def test_a_number_a_hash_and_a_url_all_resolve(ref: str) -> None:
    result = _run(["--issue", ref])

    assert result.exit_code == 0
    assert "Closes #4345" in result.output
    # A bare number still reaches the shared provider path, not a second one.
    assert result.fetch.call_args.args[0].endswith("/issues/4345")


def test_explicit_title_still_wins() -> None:
    out = _run(["--issue", "4345", "--title", "fix: something else"]).output

    assert "Title: fix: something else" in out
    assert "Closes #4345" in out


def test_without_the_flag_nothing_changes() -> None:
    """No link, and the title still comes from the goal."""
    out = _run([]).output

    assert "Closes #" not in out
    assert "resolve github issue" in out.lower()


def test_an_unreadable_issue_fails_loudly() -> None:
    result = _run(["--issue", "4345"], ticket=TicketParseError("not an issue"))

    assert result.exit_code != 0
    assert "Could not read the issue" in result.output
