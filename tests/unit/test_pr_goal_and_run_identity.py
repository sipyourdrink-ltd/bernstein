"""The PR a run opens must carry that run's identity, goal and merged work.

``bernstein pr`` reads persisted run state.  Three things were missing from
it: the goal was never written down, the run directory under ``.sdd/runs/``
was never consulted (so the session resolved to ``unknown`` and every body
section came back empty), and the title asserted a change type the linked
issue contradicted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bernstein.core.integrations.pr_gen import (
    build_pr_body,
    build_pr_title,
    load_session_summary,
)
from bernstein.core.integrations.tickets import TicketPayload
from bernstein.core.orchestration.orchestrator_cleanup import save_session_state
from bernstein.core.persistence.session import SessionState, record_run_goal, save_session

GOAL = "Give the pull request the goal its run was handed"
RUN_ID = "20260822-101500"


def _fake_orch(workdir: Path) -> Any:
    """A stand-in orchestrator with just what ``save_session_state`` reads."""
    response = MagicMock()
    response.json.return_value = [
        {"id": "T-done", "status": "done"},
        {"id": "T-open", "status": "open"},
    ]
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get.return_value = response
    return SimpleNamespace(
        _client=client,
        _config=SimpleNamespace(server_url="http://localhost:19999"),
        _cost_tracker=SimpleNamespace(spent_usd=0.25),
        _workdir=workdir,
    )


def _session_json(workdir: Path) -> dict[str, Any]:
    return json.loads((workdir / ".sdd" / "runtime" / "session.json").read_text(encoding="utf-8"))


def _write_run_dir(
    workdir: Path,
    run_id: str = RUN_ID,
    *,
    branch: str = "agent/run-1",
    rows: tuple[dict[str, Any], ...] = (),
) -> Path:
    """Write the on-disk shape a completed run leaves under ``.sdd/runs/``."""
    run_dir = workdir / ".sdd" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "started_at": time.time(),
                "git_sha": "deadbeefcafe",
                "git_branch": branch,
                "config_hash": "cfg-hash",
                "seed_path": None,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "journal.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    (run_dir / "closure-owner.json").write_text(
        json.dumps({"run_id": run_id, "pid": 4349}),
        encoding="utf-8",
    )
    return run_dir


# ---------------------------------------------------------------------------
# The goal survives the run that was given it
# ---------------------------------------------------------------------------


def test_the_goal_a_run_is_given_lands_in_the_session_file(tmp_path: Path) -> None:
    record_run_goal(tmp_path, GOAL)

    assert _session_json(tmp_path)["goal"] == GOAL


def test_a_graceful_stop_does_not_blank_the_goal(tmp_path: Path) -> None:
    record_run_goal(tmp_path, GOAL)

    save_session_state(_fake_orch(tmp_path))

    saved = _session_json(tmp_path)
    assert saved["goal"] == GOAL
    assert saved["completed_task_ids"] == ["T-done"]


def test_a_long_run_still_carries_its_goal_forward(tmp_path: Path) -> None:
    """The goal outlives the resume-staleness window; a 3-hour run keeps it."""
    save_session(tmp_path, SessionState(saved_at=time.time() - 3 * 3600, goal=GOAL))

    save_session_state(_fake_orch(tmp_path))

    assert _session_json(tmp_path)["goal"] == GOAL


def test_a_completed_runs_goal_reaches_the_pr_body(tmp_path: Path) -> None:
    record_run_goal(tmp_path, GOAL)
    save_session_state(_fake_orch(tmp_path))
    _write_run_dir(tmp_path)

    body = build_pr_body(load_session_summary(None, workdir=tmp_path))

    assert GOAL in body
    assert "no explicit goal" not in body


# ---------------------------------------------------------------------------
# The run directory is the session's identity
# ---------------------------------------------------------------------------


def test_a_run_directory_resolves_the_session_identity(tmp_path: Path) -> None:
    _write_run_dir(tmp_path, branch="agent/run-1")

    summary = load_session_summary(None, workdir=tmp_path)

    assert summary.session_id == RUN_ID
    assert summary.branch == "agent/run-1"
    assert "unknown" not in build_pr_body(summary)


def test_the_newest_run_directory_wins(tmp_path: Path) -> None:
    older = _write_run_dir(tmp_path, "20260822-090000")
    _write_run_dir(tmp_path, "20260822-101500")
    import os

    stale = time.time() - 3600
    os.utime(older, (stale, stale))

    assert load_session_summary(None, workdir=tmp_path).session_id == "20260822-101500"


def test_a_named_session_selects_its_own_run_directory(tmp_path: Path) -> None:
    _write_run_dir(tmp_path, "20260822-090000", branch="agent/older")
    _write_run_dir(tmp_path, "20260822-101500", branch="agent/newer")

    summary = load_session_summary("20260822-090000", workdir=tmp_path)

    assert summary.session_id == "20260822-090000"
    assert summary.branch == "agent/older"


def test_a_traversing_session_id_resolves_no_run_directory(tmp_path: Path) -> None:
    _write_run_dir(tmp_path)

    summary = load_session_summary("../../etc", workdir=tmp_path)

    assert summary.branch == "HEAD"


# ---------------------------------------------------------------------------
# Merged work is what the Changes section is for
# ---------------------------------------------------------------------------


def test_merged_work_reaches_the_changes_section(tmp_path: Path) -> None:
    _write_run_dir(
        tmp_path,
        rows=(
            {"event": "run_started", "run_id": RUN_ID, "git_branch": "agent/run-1"},
            {"event": "task_claimed", "task_id": "T-42", "agent_id": "eng-1"},
            {
                "event": "task_diff_captured",
                "task_id": "T-42",
                "diff_sha256": "abc123",
                "diff_added": 40,
                "diff_removed": 3,
                "diff_files": 2,
            },
            {"event": "task_merged", "task_id": "T-42", "agent_id": "eng-1"},
            {"event": "task_merged", "task_id": "T-43", "agent_id": "eng-2"},
        ),
    )

    body = build_pr_body(load_session_summary(None, workdir=tmp_path))
    changes = body.split("## Changes", 1)[1].split("## Verification", 1)[0]

    assert "T-42" in changes
    assert "+40/-3" in changes
    assert "T-43" in changes
    assert "_No changes recorded for this session._" not in changes


def test_the_projected_event_names_match_the_journal_writers() -> None:
    """``pr_gen`` names these events itself; drift would empty the section."""
    from bernstein.core.integrations import pr_gen
    from bernstein.core.replay.review_board import EVENT_TASK_DIFF_CAPTURED, EVENT_TASK_MERGED

    assert pr_gen._EVENT_TASK_MERGED == EVENT_TASK_MERGED
    assert pr_gen._EVENT_TASK_DIFF_CAPTURED == EVENT_TASK_DIFF_CAPTURED


def test_a_run_that_merged_nothing_still_says_so(tmp_path: Path) -> None:
    _write_run_dir(tmp_path, rows=({"event": "run_started", "run_id": RUN_ID},))

    body = build_pr_body(load_session_summary(None, workdir=tmp_path))

    assert "_No changes recorded for this session._" in body


# ---------------------------------------------------------------------------
# The title must not assert a type the issue contradicts
# ---------------------------------------------------------------------------


def test_an_issue_labelled_bug_does_not_get_a_feat_title() -> None:
    title = build_pr_title(
        "The goal a run was given never reaches the PR it opens",
        "engineer",
        labels=("bug",),
    )

    assert title.startswith("fix:")


def test_a_bug_label_outranks_an_enhancement_label() -> None:
    title = build_pr_title("Rework how a run reports itself", None, labels=("enhancement", "bug"))

    assert title.startswith("fix:")


def test_an_explicit_prefix_still_wins_over_a_label() -> None:
    title = build_pr_title("docs: rewrite the resume guide", "engineer", labels=("bug",))

    assert title.startswith("docs:")


def test_an_unmapped_label_leaves_the_heuristic_alone() -> None:
    title = build_pr_title("Add a resume banner", "engineer", labels=("good first issue",))

    assert title.startswith("feat:")


def test_the_cli_titles_a_labelled_bug_as_a_fix(tmp_path: Path) -> None:
    """The reported defect: a `bug` issue opened a PR titled `feat:`."""
    ticket = TicketPayload(
        id="sipyourdrink-ltd/bernstein#4349",
        title="The goal a run was given never reaches the PR it opens",
        description="body",
        labels=("bug",),
        url="https://github.com/sipyourdrink-ltd/bernstein/issues/4349",
        source="github",
    )
    from bernstein.cli.main import cli

    slug = MagicMock(stdout="sipyourdrink-ltd/bernstein\n")
    with (
        patch("bernstein.cli.commands.pr_cmd._enrich_summary_with_git", side_effect=lambda s, _w: s),
        patch("bernstein.cli.commands.pr_cmd.fetch_ticket", return_value=ticket),
        patch("bernstein.cli.commands.pr_cmd.shutil.which", return_value="/usr/bin/gh"),
        patch("bernstein.cli.commands.pr_cmd.subprocess.run", return_value=slug),
    ):
        result = CliRunner().invoke(cli, ["pr", "--dry-run", "--issue", "4349"])

    title = next(line for line in result.output.splitlines() if line.startswith("Title:"))
    assert title.startswith("Title: fix:"), title
