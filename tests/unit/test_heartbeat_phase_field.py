"""``HeartbeatStatus.phase`` reports the heartbeat's ``phase`` field and nothing else.

``HeartbeatMonitor.check`` used to compute ``phase=heartbeat.phase or
heartbeat.status``. ``status`` describes the heartbeat file's own lifecycle and
``phase`` describes the agent's work stage, so the ``or`` handed every ``phase``
consumer a value no writer had assigned as a phase (issue #3202). The
pre-spawn writer emitted only ``{"timestamp": ..., "status": "starting"}``, so
``HeartbeatStatus.phase`` came out ``"starting"`` while ``grep`` for a writer of
that phase returned nothing - a surface that disagreed with the runtime, which
is what led an earlier reading to conclude the starting-phase grace window was
dead configuration.

The fix is both halves at once: the ``or`` is gone, and the pre-spawn writer
emits the field the consumer reads. These tests pin both, plus the boundary
that must not move - the starting-phase grace window (issue #3012) still
applies to the adapter population that never overwrites the spawn-time file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bernstein.core.heartbeat import HeartbeatMonitor
from bernstein.core.models import AgentSession, ModelConfig, Task
from bernstein.core.watchdog import collect_watchdog_findings


def _write_raw_heartbeat(workdir: Path, session_id: str, payload: dict[str, Any]) -> None:
    hb_path = workdir / ".sdd" / "runtime" / "heartbeats" / f"{session_id}.json"
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    hb_path.write_text(json.dumps(payload), encoding="utf-8")


def test_prespawn_heartbeat_file_reports_the_starting_phase(tmp_path: Path) -> None:
    """The byte-exact file ``spawner_core`` writes must yield ``phase="starting"``.

    Written as the literal payload rather than by calling the spawner so the
    assertion is about the file format both sides agree on. If the writer's
    payload changes, ``test_prespawn_writer_emits_the_phase_field`` below is
    the test that fails.
    """
    _write_raw_heartbeat(tmp_path, "sess-1", {"timestamp": time.time(), "status": "starting", "phase": "starting"})

    status = HeartbeatMonitor(tmp_path).check("sess-1")

    assert status.phase == "starting"


def test_prespawn_writer_emits_the_phase_field() -> None:
    """The writer emits ``phase`` explicitly, not just ``status``.

    Pins the producer side of the contract: ``HeartbeatMonitor`` no longer
    backfills ``phase`` from ``status``, so a writer that drops the field
    silently removes its agents from the starting-phase grace window.
    """
    written: dict[str, Any] = {}

    class _Recorder:
        def mkdir(self, **_kwargs: object) -> None: ...

        def __truediv__(self, _other: str) -> _Recorder:
            return self

        def write_text(self, text: str) -> None:
            written.update(json.loads(text))

    spawner = SimpleNamespace(_workdir=_Recorder())
    from bernstein.core.agents.spawner_core import AgentSpawner

    AgentSpawner._touch_prespawn_heartbeat(spawner, "sess-1")  # type: ignore[arg-type]

    assert written.get("phase") == "starting", "the pre-spawn writer must emit the field the consumer reads"
    assert written.get("status") == "starting", "the file's own lifecycle field stays alongside it"


def test_status_no_longer_stands_in_for_an_absent_phase(tmp_path: Path) -> None:
    """A heartbeat carrying only ``status`` reports an empty phase.

    An absent ``phase`` has exactly one meaning - "the writer reported no work
    stage" - instead of silently borrowing the file's lifecycle value.
    """
    _write_raw_heartbeat(tmp_path, "sess-2", {"timestamp": time.time(), "status": "working"})

    status = HeartbeatMonitor(tmp_path).check("sess-2")

    assert status.phase == ""


def test_explicit_phase_is_reported_unchanged(tmp_path: Path) -> None:
    """A writer that does report a work stage is passed through verbatim."""
    _write_raw_heartbeat(
        tmp_path,
        "sess-3",
        {"timestamp": time.time(), "status": "working", "phase": "implementing"},
    )

    status = HeartbeatMonitor(tmp_path).check("sess-3")

    assert status.phase == "implementing"


# ---------------------------------------------------------------------------
# The boundary that must not move (issue #3012 via #3190): an agent still on
# its spawn-time heartbeat is judged against the larger starting-phase window,
# not the general stale threshold. Dropping the `or` without the writer half
# would have silently escalated this session from `high` to `critical`.
# ---------------------------------------------------------------------------


def _orch_for_watchdog(workdir: Path, session: AgentSession, task: Task) -> SimpleNamespace:
    return SimpleNamespace(
        _workdir=workdir,
        _config=SimpleNamespace(heartbeat_timeout_s=120, heartbeat_starting_timeout_s=300),
        _agents={session.id: session},
        _stall_counts={task.id: 0},
        _watchdog_log_state={},
        _latest_tasks_by_id={task.id: task},
    )


def _starting_session_findings(workdir: Path, heartbeat_age_s: float) -> list[Any]:
    """Watchdog findings for a session sitting on an un-overwritten spawn-time heartbeat."""
    now = time.time()
    session = AgentSession(
        id="sess-start",
        role="backend",
        task_ids=["T-1"],
        status="working",
        spawn_ts=now - heartbeat_age_s,
        model_config=ModelConfig("sonnet", "high"),
    )
    task = Task(id="T-1", title="Fix API", description="desc", role="backend")
    _write_raw_heartbeat(
        workdir,
        session.id,
        {"timestamp": now - heartbeat_age_s, "status": "starting", "phase": "starting"},
    )

    findings = collect_watchdog_findings(_orch_for_watchdog(workdir, session, task))
    return [f for f in findings if f.source == "heartbeat"]


def test_starting_grace_window_survives_the_phase_change(tmp_path: Path) -> None:
    """140s of heartbeat age is past the 120s general cap but below the 300s
    starting window's 150s high-water mark, so no incident is raised at all.

    Without the phase reaching the watchdog the effective timeout falls back to
    120s, the high-water mark drops to 60s, and this session is flagged.
    """
    assert _starting_session_findings(tmp_path, 140.0) == [], (
        "a session on its spawn-time heartbeat lost the starting-phase grace window: "
        "the phase field is no longer reaching the watchdog"
    )


def test_starting_phase_severity_stays_high_at_two_hundred_seconds(tmp_path: Path) -> None:
    """The exact boundary #3190 would have moved: at 200s a starting agent is
    ``high``, not ``critical``.

    Judged against the 300s starting window it is past the 150s high-water mark
    but short of the timeout. Against the general 120s threshold the same
    session reads ``critical`` and reaps, which is the outcome issue #3012
    exists to prevent.
    """
    heartbeat = _starting_session_findings(tmp_path, 200.0)

    assert len(heartbeat) == 1
    assert heartbeat[0].severity == "high"


def test_starting_grace_window_still_ends_at_its_timeout(tmp_path: Path) -> None:
    """Control: the window is a grace period, not an exemption - past 300s the
    same session is critical."""
    heartbeat = _starting_session_findings(tmp_path, 360.0)

    assert len(heartbeat) == 1
    assert heartbeat[0].severity == "critical"
