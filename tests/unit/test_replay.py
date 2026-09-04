"""Tests for task-trace replay debugging and the journal scorecard (#5402)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from bernstein.cli.advanced_cmd import replay_cmd
from bernstein.core.traces import AgentTrace, build_replay_task_request, render_replay_diff
from click.testing import CliRunner

from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.scorecard import (
    ScorecardError,
    derive_scorecard,
    derive_scorecard_from_path,
)


def _trace() -> AgentTrace:
    return AgentTrace(
        trace_id="trace-1",
        session_id="sess-1",
        task_ids=["task-1"],
        agent_role="backend",
        model="sonnet",
        effort="high",
        spawn_ts=1.0,
        task_snapshots=[
            {
                "id": "task-1",
                "title": "Fix login flow",
                "description": "Investigate the auth redirect bug.",
                "role": "backend",
                "priority": 2,
                "scope": "medium",
                "complexity": "medium",
                "result_summary": "Original result",
            }
        ],
    )


def test_build_replay_task_request_applies_model_override_and_context() -> None:
    request = build_replay_task_request(
        _trace(),
        task_id="task-1",
        override_model="opus",
        extra_context="hint: inspect the OAuth callback",
    )

    assert request.model == "opus"
    assert request.title == "[replay] Fix login flow"
    assert "hint: inspect the OAuth callback" in request.description


def test_render_replay_diff_contains_unified_diff_markers() -> None:
    diff = render_replay_diff("line one\nline two", "line one\nline three")

    assert "--- original" in diff
    assert "+++ replay" in diff
    assert "-line two" in diff
    assert "+line three" in diff


def test_replay_cli_replays_task_trace_and_renders_diff(tmp_path: Path) -> None:
    runner = CliRunner()
    with (
        patch("bernstein.cli.advanced_cmd.TraceStore.latest_for_task", return_value=_trace()),
        patch("bernstein.cli.advanced_cmd.server_post", return_value={"id": "replay-1"}),
        patch(
            "bernstein.cli.advanced_cmd.server_get",
            return_value={"id": "replay-1", "status": "done", "result_summary": "Replay result"},
        ),
    ):
        result = runner.invoke(
            replay_cmd,
            ["task-1", "--sdd-dir", str(tmp_path / ".sdd"), "--model", "opus", "--extra-context", "hint"],
        )

    assert result.exit_code == 0
    assert "Replay task created" in result.output
    assert "original" in result.output
    assert "replay" in result.output


# ---------------------------------------------------------------------------
# Scorecard projection (issue #5402)
# ---------------------------------------------------------------------------


def _scorecard_journal(sdd_dir: Path) -> EventJournal:
    """Build a journal whose every interesting event class is exercised at least once."""
    journal = EventJournal(run_id="run-sc", sdd_dir=sdd_dir)
    # Indices deliberately interleaved so a fold that mis-reads positions
    # (e.g. by looking at *all* of one event before the others) shows up
    # immediately as a wrong range.
    journal.record("run_started", run_id="run-sc", max_agents=2)
    journal.record("plan.graph.full", goal="ship it", nodes=[], task_count=0)
    journal.record("task_claimed", task_id="T-A", agent_id="a-1")
    journal.record("tool_call", task_id="T-A", tool_name="Read")
    journal.record("task_verification_failed", task_id="T-A", failed_signals=["lint"])
    journal.record("task_retried", task_id="T-A")
    journal.record("task_claimed", task_id="T-A", agent_id="a-1")
    journal.record("approval_gate", task_id="T-A", gate="deploy")
    journal.record("approval_overridden", task_id="T-A", gate="deploy")
    journal.record("task_completed", task_id="T-A", agent_id="a-1", cost_usd=0.1)
    journal.record("task_claimed", task_id="T-B", agent_id="a-2")
    journal.record("tool_call", task_id="T-B", tool_name="Edit")
    journal.record("approval_gate", task_id="T-B", gate="merge")
    journal.record("approval_honoured", task_id="T-B", gate="merge")
    journal.record("task_completed", task_id="T-B", agent_id="a-2", cost_usd=0.2)
    journal.record("run_completed", run_id="run-sc", ticks=2, outcome="completed")
    return journal


def test_scorecard_counts_tool_calls_retries_and_recoveries() -> None:
    events = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": "tool_call", "task_id": "T-A"},
        {"event": "tool_call", "task_id": "T-A"},
        {"event": "task_verification_failed", "task_id": "T-A", "failed_signals": ["lint"]},
        {"event": "task_retried", "task_id": "T-A"},
        {"event": "task_completed", "task_id": "T-A"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.tool_calls.count == 2
    assert card.tool_calls.first_index == 1
    assert card.tool_calls.last_index == 2
    assert card.retries.count == 1
    assert card.recoveries.count == 1, "retry right after a verification failure is a recovery"
    assert card.verifier_failures.count == 1
    # Both T-A tasks reach a verdict (failed then completed), so coverage is 1/1.
    assert card.verifier_coverage.count == 1


def test_scorecard_does_not_count_unrelated_retries_as_recoveries() -> None:
    events = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": "task_retried", "task_id": "T-A"},
        {"event": "task_completed", "task_id": "T-A"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.retries.count == 1
    assert card.recoveries.count == 0


def test_scorecard_separates_encountered_honoured_and_overridden_approval_gates() -> None:
    events = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": "approval_gate", "task_id": "T-A", "gate": "deploy"},
        {"event": "approval_overridden", "task_id": "T-A", "gate": "deploy"},
        {"event": "task_claimed", "task_id": "T-B"},
        {"event": "approval_gate", "task_id": "T-B", "gate": "merge"},
        {"event": "approval_honoured", "task_id": "T-B", "gate": "merge"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.approval_gates_encountered.count == 2
    assert card.approval_gates_honoured.count == 1
    assert card.approval_gates_overridden.count == 1
    # Honoured + overridden must not be folded into the encountered count:
    # both are subsets of encountered and the document must show that.
    assert (
        card.approval_gates_honoured.count + card.approval_gates_overridden.count
        <= card.approval_gates_encountered.count
    )


def test_scorecard_every_count_carries_an_event_index_range() -> None:
    events = [
        {"event": "run_started", "run_id": "r"},
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": "tool_call", "task_id": "T-A"},
        {"event": "task_verification_failed", "task_id": "T-A", "failed_signals": ["lint"]},
        {"event": "task_retried", "task_id": "T-A"},
        {"event": "task_completed", "task_id": "T-A"},
    ]
    card = derive_scorecard(events, run_id="r")
    payload = card.to_dict()

    # Every non-zero count carries a real range; the document's
    # zero-count fields carry an explicit null pair so consumers can
    # branch on the field without guessing what absence means.
    for field in ("tool_calls", "retries", "recoveries", "verifier_failures"):
        assert payload[field]["event_index_range"]["first"] is not None
        assert payload[field]["event_index_range"]["last"] is not None
    assert payload["approval_gates"]["encountered"]["event_index_range"]["first"] is None
    assert payload["approval_gates"]["encountered"]["event_index_range"]["last"] is None


def test_scorecard_is_pure_determinist_and_no_filesystem_access(monkeypatch) -> None:
    events = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": "tool_call", "task_id": "T-A"},
        {"event": "task_claimed", "task_id": "T-B"},
        {"event": "tool_call", "task_id": "T-B"},
    ]

    def _explode(*_args, **_kwargs):
        raise AssertionError("derive_scorecard must not touch the filesystem")

    monkeypatch.setattr(Path, "is_file", _explode)
    monkeypatch.setattr(Path, "read_bytes", _explode)
    monkeypatch.setattr(Path, "read_text", _explode)

    card_one = derive_scorecard(events, run_id="r")
    card_two = derive_scorecard(events, run_id="r")

    # Identical inputs produce identical documents, byte-for-byte.
    assert card_one.to_dict() == card_two.to_dict()
    assert json.dumps(card_one.to_dict(), sort_keys=True) == json.dumps(card_two.to_dict(), sort_keys=True)


def test_scorecard_rejects_an_empty_event_list() -> None:
    import pytest

    with pytest.raises(ScorecardError):
        derive_scorecard([], run_id="r")


def test_scorecard_raises_for_torn_journal_tail(tmp_path: Path) -> None:
    journal = _scorecard_journal(tmp_path / ".sdd")
    # Truncate the last JSON object mid-line so the tolerant reader has to
    # discard a trailing physical line.
    raw = journal.path.read_bytes()
    cut = raw.rfind(b"\n")
    journal.path.write_bytes(raw[: cut + 1] + b'{"event": "tool_call", "task_id":')

    import pytest

    with pytest.raises(ScorecardError) as excinfo:
        derive_scorecard_from_path(journal.path)
    assert "torn" in str(excinfo.value).lower() or "truncated" in str(excinfo.value).lower()


def test_scorecard_raises_when_journal_missing(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ScorecardError):
        derive_scorecard_from_path(tmp_path / "no-such-journal.jsonl")


def test_scorecard_derives_end_to_end_from_a_sealed_journal(tmp_path: Path) -> None:
    """End-to-end: write a journal, derive a scorecard from the file, no logs touched."""
    journal = _scorecard_journal(tmp_path / ".sdd")

    card = derive_scorecard_from_path(journal.path)

    assert card.run_id == "run-sc"
    assert card.tool_calls.count == 2
    assert card.approval_gates_encountered.count == 2
    assert card.approval_gates_honoured.count == 1
    assert card.approval_gates_overridden.count == 1
    assert card.retries.count == 1
    assert card.recoveries.count == 1
