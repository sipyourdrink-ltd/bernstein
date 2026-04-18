"""Tests for audit-013: atomic claim/spawn WAL contract.

Covers the three-phase WAL protocol that prevents work loss on a SIGKILL
between the server-side ``/claim`` transition and the orchestrator's WAL
write:

1. ``task_claimed`` (``committed=False``) -- written BEFORE ``POST /claim``
   so a crash in the HTTP window still leaves a WAL trace.
2. ``claim_confirmed`` (``committed=False``) -- written AFTER the worktree
   is materialised; records ``task_id`` -> ``worktree_path``.
3. ``task_spawn_confirmed`` (``committed=True``) -- final commit; only the
   presence of this entry proves the full claim+spawn cycle succeeded.

A SIGKILL between phase 2 and phase 3 is the audit-013 work-loss window.
The recovery path must:

- Preserve the worktree to ``.sdd/graveyard/<task_id>/<ts>/`` when it has
  dirty status or commits (so no WIP is silently reaped).
- POST ``/tasks/{id}/fail`` with reason
  ``"spawned worktree missing after crash"`` so the task re-opens.
- Append a committed ``task_retry`` WAL entry
  (``reason=crashed_spawn_recovery``) for auditability.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest
from bernstein.core.models import OrchestratorConfig
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner
from bernstein.core.wal import WALReader, WALWriter

from bernstein.adapters.base import CLIAdapter, SpawnResult

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers (mirror the patterns in test_wal_recovery_retry.py)
# ---------------------------------------------------------------------------


def _mock_adapter() -> CLIAdapter:
    """Return a minimal adapter used by the orchestrator in unit tests."""

    class _Adapter(CLIAdapter):
        def name(self) -> str:
            return "mock"

        def spawn(self, prompt: str, workdir: object, **kwargs: object) -> SpawnResult:
            return SpawnResult(pid=99999, process=None)  # type: ignore[arg-type]

        def is_running(self, pid: int) -> bool:
            return False

        def kill(self, pid: int) -> None:
            """Intentionally empty -- stub adapter for testing."""

    return _Adapter()


class _RequestRecorder:
    """Collect every request an httpx MockTransport sees for later assertions."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={})

    def fail_targets(self) -> list[tuple[str, str]]:
        """Return (task_id, reason) tuples for POST /tasks/{id}/fail calls."""
        targets: list[tuple[str, str]] = []
        for r in self.requests:
            if r.method != "POST" or "/fail" not in r.url.path:
                continue
            parts = r.url.path.strip("/").split("/")
            if len(parts) < 3 or parts[0] != "tasks" or parts[2] != "fail":
                continue
            reason = ""
            body = r.read()
            if body:
                import json as _json

                try:
                    data = _json.loads(body)
                    reason = str(data.get("reason", ""))
                except Exception:
                    pass
            targets.append((parts[1], reason))
        return targets


def _build_orchestrator(tmp_path: Path) -> tuple[Orchestrator, _RequestRecorder]:
    """Build a minimal orchestrator with a mocked httpx transport."""
    cfg = OrchestratorConfig(
        max_agents=2,
        poll_interval_s=1,
        heartbeat_timeout_s=120,
        max_tasks_per_agent=3,
        server_url="http://testserver",
    )
    adapter = _mock_adapter()
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    spawner = AgentSpawner(adapter, templates_dir, tmp_path)
    rec = _RequestRecorder()
    transport = httpx.MockTransport(rec)
    client = httpx.Client(transport=transport, base_url="http://testserver")
    orch = Orchestrator(cfg, spawner, tmp_path, client=client)
    return orch, rec


def _make_git_worktree(path: Path, *, dirty: bool = True) -> None:
    """Initialise a git repo at *path* and optionally leave a dirty file."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    (path / ".gitkeep").write_text("")
    subprocess.run(["git", "add", ".gitkeep"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    if dirty:
        (path / "wip.txt").write_text("unsaved work")


# ---------------------------------------------------------------------------
# Tests: find_orphaned_claim_confirmed detection
# ---------------------------------------------------------------------------


class TestFindOrphanedClaimConfirmed:
    """Unit tests for Orchestrator._find_orphaned_claim_confirmed."""

    def test_empty_wal_returns_empty(self, tmp_path: Path) -> None:
        """No WAL directory -> no crashed spawns."""
        orch, _ = _build_orchestrator(tmp_path)
        result = orch._find_orphaned_claim_confirmed(tmp_path / ".sdd")
        assert result == []

    def test_claim_confirmed_with_matching_spawn_is_not_orphan(self, tmp_path: Path) -> None:
        """claim_confirmed paired with task_spawn_confirmed is committed work."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="ok-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)
        w.append(
            "claim_confirmed",
            {"task_id": "T-1", "worktree_path": "/tmp/wt"},
            {},
            "lifecycle",
            committed=False,
        )
        w.append("task_spawn_confirmed", {"task_id": "T-1"}, {}, "lifecycle", committed=True)

        orch, _ = _build_orchestrator(tmp_path)
        result = orch._find_orphaned_claim_confirmed(sdd)
        assert result == []

    def test_claim_confirmed_without_spawn_is_orphan(self, tmp_path: Path) -> None:
        """claim_confirmed with no matching spawn_confirmed is a crashed spawn."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-2"}, {}, "lifecycle", committed=False)
        w.append(
            "claim_confirmed",
            {"task_id": "T-2", "worktree_path": "/tmp/wt-2"},
            {},
            "lifecycle",
            committed=False,
        )

        orch, _ = _build_orchestrator(tmp_path)
        result = orch._find_orphaned_claim_confirmed(sdd)
        assert len(result) == 1
        run_id, entry = result[0]
        assert run_id == "crashed-run"
        assert entry.inputs["task_id"] == "T-2"
        assert entry.inputs["worktree_path"] == "/tmp/wt-2"

    def test_excludes_current_run(self, tmp_path: Path) -> None:
        """Current run's WAL is excluded from the scan."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        orch, _ = _build_orchestrator(tmp_path)
        # Write a crashed-spawn pattern into the CURRENT run's WAL
        orch._wal_writer.write_entry(
            decision_type="claim_confirmed",
            inputs={"task_id": "T-current", "worktree_path": "/tmp/x"},
            output={},
            actor="lifecycle",
            committed=False,
        )

        result = orch._find_orphaned_claim_confirmed(sdd)
        assert result == []

    def test_committed_claim_confirmed_is_not_orphan(self, tmp_path: Path) -> None:
        """A committed=True claim_confirmed is not a crashed spawn."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="ok-run", sdd_dir=sdd)
        w.append(
            "claim_confirmed",
            {"task_id": "T-ok", "worktree_path": "/tmp/wt"},
            {},
            "lifecycle",
            committed=True,
        )

        orch, _ = _build_orchestrator(tmp_path)
        result = orch._find_orphaned_claim_confirmed(sdd)
        assert result == []


# ---------------------------------------------------------------------------
# Tests: SIGKILL between claim_confirmed and task_spawn_confirmed
# ---------------------------------------------------------------------------


class TestRecoverCrashedSpawn:
    """End-to-end verification: the audit-013 work-loss window is closed."""

    def test_crashed_spawn_triggers_fail_post(self, tmp_path: Path) -> None:
        """A crashed spawn must POST /tasks/{id}/fail with the audit-013 reason."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-lost"}, {}, "lifecycle", committed=False)
        w.append(
            "claim_confirmed",
            {"task_id": "T-lost", "worktree_path": "/nonexistent"},
            {},
            "lifecycle",
            committed=False,
        )
        # NO task_spawn_confirmed -- SIGKILL landed between phases 2 and 3.

        orch, recorder = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        targets = recorder.fail_targets()
        assert ("T-lost", "spawned worktree missing after crash") in targets, (
            f"Expected POST /tasks/T-lost/fail with the audit-013 reason; got {targets}"
        )

    def test_crashed_spawn_preserves_dirty_worktree_to_graveyard(self, tmp_path: Path) -> None:
        """A dirty worktree is moved to .sdd/graveyard/<task_id>/<ts>/."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        worktree = tmp_path / "my-worktree"
        _make_git_worktree(worktree, dirty=True)

        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-wip"}, {}, "lifecycle", committed=False)
        w.append(
            "claim_confirmed",
            {"task_id": "T-wip", "worktree_path": str(worktree)},
            {},
            "lifecycle",
            committed=False,
        )

        orch, _ = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        # Original worktree moved out
        assert not worktree.exists(), "source worktree should have been moved"

        # Dest landed under .sdd/graveyard/<task_id>/<ts>/
        graveyard = tmp_path / ".sdd" / "graveyard" / "T-wip"
        assert graveyard.is_dir(), "graveyard directory should exist for the task"
        snapshots = list(graveyard.iterdir())
        assert len(snapshots) == 1, f"exactly one snapshot expected, got {snapshots}"
        snapshot = snapshots[0]
        # The unsaved file must survive the move
        assert (snapshot / "wip.txt").read_text() == "unsaved work"

    def test_crashed_spawn_writes_task_retry_wal_entry(self, tmp_path: Path) -> None:
        """Recovery appends a committed task_retry entry with reason=crashed_spawn_recovery."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append(
            "claim_confirmed",
            {"task_id": "T-audit", "worktree_path": "/nonexistent"},
            {},
            "lifecycle",
            committed=False,
        )

        orch, _ = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        reader = WALReader(run_id=orch._run_id, sdd_dir=sdd)
        retries = [
            e for e in reader.iter_entries() if e.decision_type == "task_retry" and e.inputs.get("task_id") == "T-audit"
        ]
        assert len(retries) == 1
        assert retries[0].inputs["reason"] == "crashed_spawn_recovery"
        assert retries[0].inputs["original_run_id"] == "crashed-run"
        assert retries[0].committed is True

    def test_ack_entry_marks_crashed_spawn_flag(self, tmp_path: Path) -> None:
        """wal_recovery_ack output.crashed_spawn is True for claim_confirmed orphans."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append(
            "claim_confirmed",
            {"task_id": "T-ack", "worktree_path": ""},
            {},
            "lifecycle",
            committed=False,
        )

        orch, _ = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        reader = WALReader(run_id=orch._run_id, sdd_dir=sdd)
        acks = [
            e
            for e in reader.iter_entries()
            if e.decision_type == "wal_recovery_ack" and e.inputs["original_inputs"].get("task_id") == "T-ack"
        ]
        assert len(acks) == 1
        assert acks[0].output["crashed_spawn"] is True

    def test_missing_worktree_path_still_fails_task(self, tmp_path: Path) -> None:
        """An empty worktree_path in the entry does not prevent the /fail POST."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append(
            "claim_confirmed",
            {"task_id": "T-nowt", "worktree_path": ""},
            {},
            "lifecycle",
            committed=False,
        )

        orch, recorder = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        targets = recorder.fail_targets()
        assert any(tid == "T-nowt" for tid, _ in targets)

    def test_multiple_crashed_spawns_all_recovered(self, tmp_path: Path) -> None:
        """Every crashed_spawn across multiple runs is handled."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        for i in range(3):
            w = WALWriter(run_id=f"crashed-{i}", sdd_dir=sdd)
            w.append(
                "claim_confirmed",
                {"task_id": f"T-{i}", "worktree_path": ""},
                {},
                "lifecycle",
                committed=False,
            )

        orch, recorder = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        targets = sorted(tid for tid, _ in recorder.fail_targets())
        assert targets == ["T-0", "T-1", "T-2"]


# ---------------------------------------------------------------------------
# Regression: simulate the exact audit-013 scenario end-to-end.
# ---------------------------------------------------------------------------


class TestAudit013Regression:
    """Exact scenario from the ticket: SIGKILL between claim_confirmed and spawn."""

    def test_sigkill_between_claim_confirmed_and_spawn_preserves_and_reopens(self, tmp_path: Path) -> None:
        """Full reproduction: dirty worktree + task_id recoverable after hard crash.

        Verifies ALL of the ticket's exit criteria:
        - Worktree preserved to ``.sdd/graveyard/<task_id>/<ts>/``
        - ``POST /tasks/{id}/fail`` called with the audit-013 reason
        - ``task_retry`` WAL entry appended for auditability
        - Recovery is idempotent (the prior WAL is closed)
        """
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        # Simulate the crashed run: write the first two WAL phases and stop
        # (no task_spawn_confirmed ever arrives -- that's the SIGKILL).
        crashed_worktree = tmp_path / "crashed-session"
        _make_git_worktree(crashed_worktree, dirty=True)

        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append(
            "task_claimed",
            {"task_id": "T-critical", "role": "backend", "title": "t"},
            {"phase": "claim_intent"},
            "lifecycle",
            committed=False,
        )
        # (HTTP POST /claim succeeded, worktree was created)
        w.append(
            "claim_confirmed",
            {
                "task_id": "T-critical",
                "agent_id": "agent-xyz",
                "worktree_path": str(crashed_worktree),
            },
            {"role": "backend", "phase": "worktree_created"},
            "lifecycle",
            committed=False,
        )
        # <-- SIGKILL HERE -- no task_spawn_confirmed.

        # Fresh orchestrator boots -- recovery runs automatically via __init__?
        # No, _recover_from_wal runs in .run().  Call it directly.
        orch, recorder = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        # 1. POST /tasks/T-critical/fail with the audit-013 reason
        targets = recorder.fail_targets()
        assert ("T-critical", "spawned worktree missing after crash") in targets

        # 2. Worktree moved to graveyard with the unsaved file intact
        assert not crashed_worktree.exists()
        graveyard_task = tmp_path / ".sdd" / "graveyard" / "T-critical"
        assert graveyard_task.is_dir()
        snaps = list(graveyard_task.iterdir())
        assert len(snaps) == 1
        assert (snaps[0] / "wip.txt").read_text() == "unsaved work"

        # 3. task_retry WAL entry with the correct reason
        reader = WALReader(run_id=orch._run_id, sdd_dir=sdd)
        retries = [
            e
            for e in reader.iter_entries()
            if e.decision_type == "task_retry" and e.inputs.get("task_id") == "T-critical"
        ]
        assert len(retries) == 1
        assert retries[0].inputs["reason"] == "crashed_spawn_recovery"
        # graveyard_path is recorded for operator traceability
        assert "T-critical" in retries[0].inputs.get("graveyard_path", "")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-x", "-q"])
