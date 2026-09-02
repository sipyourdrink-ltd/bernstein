"""Hermetic coordination re-derivation over a recorded run journal (issue #4213).

``bernstein replay <run> --verify`` recomputes the Merkle chain over the rows
that are already on disk: it proves the journal was not edited, not that the
coordination sequence it records is the sequence the scheduler's rules produce.
These tests pin the second property - every coordination step is re-derived
from the recorded plan and the recorded per-task outcomes, and the re-derived
journal's timing-excluded head is compared to the recorded head.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest

from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.replay.rederive import (
    REASON_CODE_HEAD_MISMATCH,
    REASON_CODE_PLAN_MISSING,
    REASON_CODE_UNDERIVABLE_STEP,
    RULE_DEPENDENCY_NOT_COMPLETED,
    RULE_OUTCOME_FOR_UNCLAIMED_TASK,
    rederive_run,
)

if TYPE_CHECKING:
    from pathlib import Path


def _plan_nodes() -> list[dict[str, object]]:
    """A two-node plan where ``T-2`` depends on ``T-1``."""
    return [
        {"id": "T-1", "role": "backend", "title": "first", "depends_on": []},
        {"id": "T-2", "role": "backend", "title": "second", "depends_on": ["T-1"]},
    ]


def _recorded_run(sdd_dir: Path, run_id: str = "run-1") -> EventJournal:
    """Record a two-task run whose coordination obeys the dependency edge."""
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    journal.record("run_started", run_id=run_id, max_agents=2)
    journal.record("plan.graph.full", goal="ship it", nodes=_plan_nodes(), task_count=2)
    journal.record("tick_start", tick=1)
    journal.record("agent_spawned", agent_id="a-1", role="backend", task_ids=["T-1"])
    journal.record("task_claimed", task_id="T-1", agent_id="a-1")
    journal.record("task_completed", task_id="T-1", agent_id="a-1", cost_usd=0.1)
    journal.record("tick_start", tick=2)
    journal.record("agent_spawned", agent_id="a-2", role="backend", task_ids=["T-2"])
    journal.record("task_claimed", task_id="T-2", agent_id="a-2")
    journal.record("task_completed", task_id="T-2", agent_id="a-2", cost_usd=0.2)
    journal.record("run_completed", run_id=run_id, ticks=2, outcome="completed")
    return journal


def _rewrite(journal_path: Path, index: int, **overrides: object) -> None:
    """Overwrite payload fields of the recorded row at *index*, in place.

    The chain fields are left as recorded: the point of an injected outcome
    is that the operator is handed a journal that looks untouched at a glance.
    """
    import json

    lines = journal_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[index])
    row.update(overrides)
    lines[index] = json.dumps(row)
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _drop(journal_path: Path, index: int) -> None:
    """Delete the recorded row at *index*."""
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    del lines[index]
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# 1 (load-bearing) --------------------------------------------------------


def test_untouched_run_rederives_to_the_recorded_timing_excluded_head(tmp_path: Path) -> None:
    """Re-deriving an untouched run reproduces the recorded head exactly."""
    journal = _recorded_run(tmp_path / ".sdd")

    result = rederive_run(journal.path, sandbox=tmp_path / "sandbox")

    assert result.ok is True
    assert result.divergent_index is None
    assert result.derived_head == result.recorded_head == journal.head()
    assert result.step_count == len(load_events(journal.path).events)


# 2 -----------------------------------------------------------------------


def test_rederived_head_is_independent_of_the_recorded_wall_clock(tmp_path: Path) -> None:
    """Rewriting only the timing envelope leaves the re-derived head unchanged."""
    journal = _recorded_run(tmp_path / ".sdd")
    baseline = rederive_run(journal.path, sandbox=tmp_path / "sandbox-a")

    for index in range(len(load_events(journal.path).events)):
        _rewrite(journal.path, index, ts=1.0, elapsed_s=0.0)

    shifted = rederive_run(journal.path, sandbox=tmp_path / "sandbox-b")

    assert shifted.derived_head == baseline.derived_head
    assert shifted.ok is True


# 3 -----------------------------------------------------------------------


def test_flipping_a_recorded_outcome_names_the_first_underivable_step(tmp_path: Path) -> None:
    """A dependency demoted to a failure makes the dependent's claim underivable."""
    journal = _recorded_run(tmp_path / ".sdd")
    # Row 5 is ``task_completed`` for T-1; demote it to a verification failure.
    _rewrite(journal.path, 5, event="task_verification_failed", failed_signals=["tests"])

    result = rederive_run(journal.path, sandbox=tmp_path / "sandbox")

    assert result.ok is False
    assert result.reason_code == REASON_CODE_UNDERIVABLE_STEP
    assert result.rule == RULE_DEPENDENCY_NOT_COMPLETED
    # Row 8 is the ``task_claimed`` for T-2, which no longer has a completed dep.
    assert result.divergent_index == 8
    assert "T-2" in result.reason
    assert "T-1" in result.reason


# 4 -----------------------------------------------------------------------


def test_outcome_for_a_task_that_was_never_claimed_is_refused(tmp_path: Path) -> None:
    """An outcome with no preceding claim is not a step the tick loop can produce."""
    journal = _recorded_run(tmp_path / ".sdd")
    _drop(journal.path, 4)  # the ``task_claimed`` for T-1

    result = rederive_run(journal.path, sandbox=tmp_path / "sandbox")

    assert result.ok is False
    assert result.reason_code == REASON_CODE_UNDERIVABLE_STEP
    assert result.rule == RULE_OUTCOME_FOR_UNCLAIMED_TASK
    assert result.divergent_index == 4


# 5 -----------------------------------------------------------------------


def test_claim_before_its_dependency_completed_is_refused(tmp_path: Path) -> None:
    """The dependency edge in the recorded plan gates every recorded claim."""
    sdd_dir = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-early", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="run-early", max_agents=2)
    journal.record("plan.graph.full", goal="ship it", nodes=_plan_nodes(), task_count=2)
    journal.record("task_claimed", task_id="T-2", agent_id="a-1")

    result = rederive_run(journal.path, sandbox=tmp_path / "sandbox")

    assert result.ok is False
    assert result.rule == RULE_DEPENDENCY_NOT_COMPLETED
    assert result.divergent_index == 2


# 6 -----------------------------------------------------------------------


def test_edited_outcome_payload_diverges_at_the_named_step(tmp_path: Path) -> None:
    """A payload edit that keeps the sequence derivable still moves the head."""
    journal = _recorded_run(tmp_path / ".sdd")
    _rewrite(journal.path, 5, cost_usd=99.0)

    result = rederive_run(journal.path, sandbox=tmp_path / "sandbox")

    assert result.ok is False
    assert result.reason_code == REASON_CODE_HEAD_MISMATCH
    assert result.derived_head != result.recorded_head
    assert result.divergent_index == 5


# 7 -----------------------------------------------------------------------


def test_run_without_a_recorded_plan_is_refused_by_name(tmp_path: Path) -> None:
    """No recorded planning output means there is nothing to re-derive from."""
    sdd_dir = tmp_path / ".sdd"
    journal = EventJournal(run_id="run-planless", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="run-planless", max_agents=1)
    journal.record("run_completed", run_id="run-planless", ticks=0, outcome="completed")

    result = rederive_run(journal.path, sandbox=tmp_path / "sandbox")

    assert result.ok is False
    assert result.reason_code == REASON_CODE_PLAN_MISSING
    assert result.divergent_index is None


# 8 -----------------------------------------------------------------------


def test_rederivation_opens_no_socket_and_needs_no_executable_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-derivation is hermetic: no adapter binary, no network (AC3)."""
    journal = _recorded_run(tmp_path / ".sdd")

    monkeypatch.setenv("PATH", "")

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("re-derivation opened a socket")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)

    result = rederive_run(journal.path, sandbox=tmp_path / "sandbox")

    assert result.ok is True


# 9 -----------------------------------------------------------------------


def test_sandbox_is_the_only_tree_the_rederivation_writes(tmp_path: Path) -> None:
    """The fresh journal lands in the sandbox; the recorded run dir is untouched."""
    journal = _recorded_run(tmp_path / ".sdd")
    before = sorted(p.name for p in journal.path.parent.iterdir())

    sandbox = tmp_path / "sandbox"
    result = rederive_run(journal.path, sandbox=sandbox)

    assert result.derived_journal_path.startswith(str(sandbox))
    assert sorted(p.name for p in journal.path.parent.iterdir()) == before
