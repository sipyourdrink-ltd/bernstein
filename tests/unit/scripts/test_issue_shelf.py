"""The shelf labeller must be wrong in only one direction: it may leave work
un-advertised, never advertise work somebody already holds.

Each test is named for the way the shelf can lie, not for the function it calls.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "issue_shelf", Path(__file__).resolve().parents[3] / "scripts" / "issue_shelf.py"
)
assert _SPEC and _SPEC.loader
issue_shelf = importlib.util.module_from_spec(_SPEC)
sys.modules["issue_shelf"] = issue_shelf
_SPEC.loader.exec_module(issue_shelf)

Issue = issue_shelf.Issue
Repo = issue_shelf.Repo
decide = issue_shelf.decide

READY_BODY = """## Problem
`src/bernstein/core/orchestration/worker.py` drops the value.

## Where to start
Start at `worker.py:120`.

## Acceptance criteria
- [ ] a test covers it
"""


def _issue(**kw) -> Issue:
    base = {
        "number": 1,
        "labels": frozenset({"size/s", "enhancement"}),
        "assignees": (),
        "milestone": "v4.0.0",
        "body": READY_BODY,
        "last_outside_comment_days": None,
    }
    base.update(kw)
    return Issue(**base)


# --- the direction that costs a contributor an evening --------------------------------


def test_an_assigned_issue_loses_its_bait() -> None:
    d = decide(_issue(labels=frozenset({"size/s", "up-for-grabs", "help wanted"}), assignees=("dev",)), Repo())
    assert d.action == "UNBAIT"
    assert set(d.remove) == {"up-for-grabs", "help wanted"}


def test_an_issue_an_open_pr_closes_loses_its_bait() -> None:
    d = decide(_issue(labels=frozenset({"size/s", "up-for-grabs"})), Repo(claimed_by_pr=frozenset({1})))
    assert d.action == "UNBAIT"


def test_a_lane_running_the_issue_counts_as_taken() -> None:
    """The queue lives outside GitHub; without the dispatcher's label an automated run
    is invisible here and the shelf would advertise work already in progress."""
    d = decide(_issue(labels=frozenset({"size/s", "up-for-grabs", "fleet-running"})), Repo())
    assert d.action == "UNBAIT"


def test_a_tracking_issue_is_never_advertised() -> None:
    assert decide(_issue(labels=frozenset({"size/s", "roadmap"})), Repo()).action != "BAIT"


@pytest.mark.parametrize("hold", ["reserved", "blocked", "needs-better-brief", "fleet-blocked"])
def test_a_held_issue_is_never_advertised(hold: str) -> None:
    assert decide(_issue(labels=frozenset({"size/s", hold})), Repo()).action != "BAIT"


# --- the ambiguous middle, which must move nothing -------------------------------------


def test_a_recent_outside_comment_moves_nothing_in_either_direction() -> None:
    d = decide(_issue(labels=frozenset({"size/s", "up-for-grabs"}), last_outside_comment_days=2.0), Repo())
    assert d.action == "UNPROVEN"
    assert not d.add and not d.remove


def test_an_old_outside_comment_stops_blocking_the_shelf() -> None:
    d = decide(_issue(last_outside_comment_days=90.0), Repo())
    assert d.action == "BAIT"


# --- the human always wins --------------------------------------------------------------


def test_a_human_veto_is_never_overridden() -> None:
    d = decide(_issue(labels=frozenset({"size/s", "no-bait"})), Repo())
    assert d.action == "LEAVE"
    assert not d.add


# --- what may be advertised -------------------------------------------------------------


def test_a_free_sized_milestoned_briefed_issue_is_advertised() -> None:
    d = decide(_issue(), Repo())
    assert d.action == "BAIT"
    assert {"help wanted", "up-for-grabs"} <= set(d.add)


def test_a_large_issue_is_left_alone_rather_than_advertised() -> None:
    d = decide(_issue(labels=frozenset({"size/l"})), Repo())
    assert d.action == "LEAVE"
    assert "shelf-sized" in d.reason


def test_an_unsized_issue_is_left_alone() -> None:
    d = decide(_issue(labels=frozenset({"enhancement"})), Repo())
    assert d.action == "LEAVE" and "no size label" in d.reason


def test_an_issue_without_acceptance_criteria_is_left_alone() -> None:
    d = decide(_issue(body="## Problem\n`worker.py` drops it.\n"), Repo())
    assert d.action == "LEAVE" and "acceptance" in d.reason


def test_an_issue_naming_no_path_or_symbol_is_left_alone() -> None:
    d = decide(_issue(body="## Acceptance criteria\n- [ ] it works\n"), Repo())
    assert d.action == "LEAVE" and "path or symbol" in d.reason


def test_beginner_labels_need_both_a_small_size_and_a_starting_point() -> None:
    small = decide(_issue(labels=frozenset({"size/xs"})), Repo())
    assert {"good first issue", "beginner-friendly"} <= set(small.add)
    medium = decide(_issue(labels=frozenset({"size/m"})), Repo())
    assert "good first issue" not in medium.add


def test_no_starting_point_means_no_beginner_label() -> None:
    body = READY_BODY.replace("## Where to start\nStart at `worker.py:120`.\n", "")
    d = decide(_issue(labels=frozenset({"size/xs"}), body=body), Repo())
    assert d.action == "BAIT" and "good first issue" not in d.add


def test_a_correctly_advertised_issue_produces_no_write() -> None:
    """Anti-flap: the job must not rewrite labels that already say the right thing."""
    already = frozenset({"size/s", "help wanted", "up-for-grabs", "good first issue", "beginner-friendly"})
    d = decide(_issue(labels=already), Repo())
    assert d.action == "LEAVE" and not d.add and not d.remove


def test_losing_a_milestone_does_not_silently_pull_a_baited_issue_off_the_shelf() -> None:
    d = decide(_issue(labels=frozenset({"size/s", "up-for-grabs"}), milestone=None), Repo())
    assert d.action == "LEAVE"
    assert not d.remove


# --- the two spellings of a bot ---------------------------------------------------------


def test_a_bot_is_recognised_by_type_not_by_a_suffix_in_its_login() -> None:
    """GraphQL returns an app's login without the `[bot]` suffix REST shows, so a
    suffix check read four issues the orchestrator had just commented on as claimed by
    an outside contributor and refused to shelve any of them."""
    graphql_spelling = {"login": "bernstein-orchestrator", "type": "Bot"}
    rest_spelling = {"login": "bernstein-orchestrator[bot]", "type": "Bot"}
    assert not issue_shelf.is_outside_human(graphql_spelling, "chernistry")
    assert not issue_shelf.is_outside_human(rest_spelling, "chernistry")


def test_the_maintainer_is_not_an_outside_contributor() -> None:
    assert not issue_shelf.is_outside_human({"login": "chernistry", "type": "User"}, "chernistry")


def test_a_contributor_is_an_outside_contributor() -> None:
    assert issue_shelf.is_outside_human({"login": "vaibhav8a", "type": "User"}, "chernistry")


def test_a_hold_label_the_repo_does_not_define_is_reported() -> None:
    """A hold whose label does not exist can never be on an issue, so the branch that
    reads it never runs -- the guard looks present in the source and is inert in the run."""
    with mock.patch.object(issue_shelf, "_gh", return_value='[{"name": "reserved"}, {"name": "blocked"}]'):
        assert issue_shelf.missing_control_labels("o/r") == [
            "fleet-blocked",
            "fleet-running",
            "needs-better-brief",
            "no-bait",
        ]


def test_the_veto_label_counts_as_a_control_label() -> None:
    """Without `no-bait` a maintainer cannot say leave this one alone, and the next run
    re-advertises whatever they just took down."""
    labels = json.dumps([{"name": name} for name in issue_shelf.HOLDS])
    with mock.patch.object(issue_shelf, "_gh", return_value=labels):
        assert issue_shelf.missing_control_labels("o/r") == [issue_shelf.VETO]


def test_a_repo_defining_every_control_label_reports_nothing_missing() -> None:
    labels = json.dumps([{"name": name} for name in issue_shelf.HOLDS | {issue_shelf.VETO}])
    with mock.patch.object(issue_shelf, "_gh", return_value=labels):
        assert issue_shelf.missing_control_labels("o/r") == []
