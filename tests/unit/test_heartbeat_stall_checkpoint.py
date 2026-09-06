"""Invariant: every automatic stall kill writes a resumable checkpoint first.

Issue #3376: a worker stalled and killed by any of the three escalation paths
in ``heartbeat`` (``_escalate_heartbeat``, ``_escalate_stall_simple``,
``_escalate_stall_profiled``) used to lose all of its state -- the only
artifacts were a signal file and a log line. ``bernstein resume`` already
reads a per-task checkpoint written after a normal step completion, but
nothing wrote one at the kill boundary, so a stalled task could never be
resumed.

These tests drive the real kill paths (never mocking the write itself, except
where the test is specifically about the fail-open contract) and assert on
the checkpoint that lands on disk -- the same file ``bernstein resume``
reads -- proving the write happens before the kill, carries the stall
reason, and never blocks the kill when it fails.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.cli.commands.resume_cmd import prepare_resume
from bernstein.core.agents.heartbeat import (
    StallProfile,
    _escalate_heartbeat,
    _escalate_stall_profiled,
    _escalate_stall_simple,
)
from bernstein.core.defaults import AGENT
from bernstein.core.orchestration.supervisor_receipt import StallReason
from bernstein.core.persistence.task_resume import load_checkpoint
from bernstein.core.tasks.models import AgentSession, ModelConfig

RUN_ID = "run-stall-checkpoint-1"


def _session(sid: str = "A-1", task_id: str = "T-1", *, pid: int | None = 4321) -> AgentSession:
    return AgentSession(
        id=sid,
        role="backend",
        task_ids=[task_id],
        status="working",
        spawn_ts=100.0,
        pid=pid,
        model_config=ModelConfig("sonnet", "high"),
    )


def _orch(workdir: Path, session: AgentSession) -> SimpleNamespace:
    worktree = workdir / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    spawner = MagicMock()
    spawner.get_worktree_path.return_value = worktree
    spawner.default_adapter_name = "claude"
    return SimpleNamespace(
        _agents={session.id: session},
        _workdir=workdir,
        _run_id=RUN_ID,
        _signal_mgr=MagicMock(),
        _spawner=spawner,
        _stall_counts={},
    )


# ---------------------------------------------------------------------------
# Every detector writes a checkpoint that parses
# ---------------------------------------------------------------------------


def test_heartbeat_kill_writes_a_resumable_checkpoint(tmp_path: Path) -> None:
    """A heartbeat-stale kill writes a checkpoint naming HEARTBEAT_STALE."""
    session = _session()
    orch = _orch(tmp_path, session)
    with patch("bernstein.core.platform_compat.kill_process_group"):
        _escalate_heartbeat(
            orch,
            session,
            age=950.0,
            elapsed=850.0,
            shutdown_threshold=60.0,
            wakeup_threshold=30.0,
            shutdown_reason="no_heartbeat",
            kill_threshold=90.0,
        )

    cp = load_checkpoint(tmp_path, "T-1")
    assert cp.stall_reason == StallReason.HEARTBEAT_STALE.value
    assert cp.adapter == "claude"
    assert cp.worktree_path == str(tmp_path / "worktree")


def test_stall_simple_kill_writes_a_resumable_checkpoint(tmp_path: Path) -> None:
    """An identical-progress kill (no-workdir detector) writes a checkpoint."""
    session = _session(sid="B-1", task_id="T-2")
    orch = _orch(tmp_path, session)
    _escalate_stall_simple(orch, session, "T-2", count=AGENT.escalation_kill_count)

    cp = load_checkpoint(tmp_path, "T-2")
    assert cp.stall_reason == StallReason.NO_PROGRESS.value
    assert cp.adapter == "claude"


def test_stall_profiled_kill_writes_a_resumable_checkpoint(tmp_path: Path) -> None:
    """An identical-progress kill (profiled detector) writes a checkpoint."""
    session = _session(sid="C-1", task_id="T-3")
    orch = _orch(tmp_path, session)
    profile = StallProfile(wakeup_threshold=3, shutdown_threshold=5, kill_threshold=7, reason="default profile")
    _escalate_stall_profiled(orch, session, "T-3", count=7, profile=profile)

    cp = load_checkpoint(tmp_path, "T-3")
    assert cp.stall_reason == StallReason.NO_PROGRESS.value


# ---------------------------------------------------------------------------
# Ordering: checkpoint is written before the kill signal
# ---------------------------------------------------------------------------


def test_checkpoint_exists_before_the_kill_signal(tmp_path: Path) -> None:
    """At the moment the kill runs the checkpoint is already on disk."""
    session = _session(sid="D-1", task_id="T-4")
    orch = _orch(tmp_path, session)

    seen: dict[str, bool] = {}

    def fake_kill(_session: object) -> None:
        try:
            load_checkpoint(tmp_path, "T-4")
            seen["checkpoint_present_at_kill"] = True
        except Exception:
            seen["checkpoint_present_at_kill"] = False

    orch._spawner.kill.side_effect = fake_kill
    _escalate_stall_simple(orch, session, "T-4", count=AGENT.escalation_kill_count)

    assert seen["checkpoint_present_at_kill"] is True
    assert orch._spawner.kill.called


# ---------------------------------------------------------------------------
# Fail-open: a checkpoint write failure never blocks the kill
# ---------------------------------------------------------------------------


def test_checkpoint_write_failure_never_blocks_the_kill(tmp_path: Path) -> None:
    """A raising checkpoint write leaves the kill issued and warns loudly."""
    session = _session(sid="E-1", task_id="T-5")
    orch = _orch(tmp_path, session)

    with (
        patch(
            "bernstein.core.tasks.task_lifecycle._write_task_resume_checkpoint",
            side_effect=RuntimeError("disk full"),
        ),
        patch("bernstein.core.agents.heartbeat.logger") as log,
    ):
        _escalate_stall_simple(orch, session, "T-5", count=AGENT.escalation_kill_count)

    assert orch._spawner.kill.called
    warnings = [c for c in log.warning.call_args_list if "Could not write stall checkpoint" in str(c.args[0])]
    assert warnings, "checkpoint write failure must be logged, not silent"
    assert "E-1" in warnings[0].args


def test_no_workdir_skips_checkpoint_without_blocking_kill(tmp_path: Path) -> None:
    """No workdir on the orchestrator -> checkpoint write is a silent no-op."""
    session = _session(sid="F-1", task_id="T-6")
    orch = _orch(tmp_path, session)
    orch._workdir = None  # simulates the no-workdir simple-mode caller

    _escalate_stall_simple(orch, session, "T-6", count=AGENT.escalation_kill_count)

    assert orch._spawner.kill.called
    assert not (tmp_path / ".sdd" / "runtime" / "checkpoints" / "T-6").exists()


# ---------------------------------------------------------------------------
# The load-bearing test: `bernstein resume` actually succeeds afterwards
# ---------------------------------------------------------------------------


def test_resume_succeeds_against_a_stall_kill_checkpoint(tmp_path: Path) -> None:
    """Before this change, `bernstein resume` could never succeed against a
    stalled task: nothing wrote its checkpoint. It now does."""
    session = _session(sid="G-1", task_id="T-7")
    orch = _orch(tmp_path, session)
    _escalate_stall_profiled(
        orch,
        session,
        "T-7",
        count=7,
        profile=StallProfile(wakeup_threshold=3, shutdown_threshold=5, kill_threshold=7, reason="stuck"),
    )

    plan = prepare_resume(tmp_path, "T-7")

    assert plan.checkpoint.task_id == "T-7"
    assert plan.checkpoint.stall_reason == StallReason.NO_PROGRESS.value
    assert plan.checkpoint.resume_count == 1
    assert "Resume context" in plan.resume_context
