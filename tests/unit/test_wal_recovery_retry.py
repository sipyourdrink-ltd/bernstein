"""Tests for audit-001: WAL recovery must retry orphaned claims.

Covers the fix for the silent work-loss window where ``_recover_from_wal``
previously logged and acked uncommitted ``task_claimed`` entries without
ever re-queuing them.  With the fix, each orphaned claim (no matching
``task_spawn_confirmed``) must:

1. Trigger ``POST /tasks/{id}/force-claim`` with ``reason=crash_recovery``.
2. Append a ``task_retry`` entry to the current run's WAL.
3. Leave a ``wal_recovery_ack`` trail flagged with ``orphan=True``.

Worktree preservation is also exercised: prior-run worktrees with a dirty
``git status`` are moved to ``.sdd/worktrees/preserved/`` and a bulletin
message is emitted so an operator / fresh agent can resume the work.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from bernstein.core.models import OrchestratorConfig
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner
from bernstein.core.wal import WALReader, WALRecovery, WALWriter

from bernstein.adapters.base import CLIAdapter, SpawnResult

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
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

    def force_claim_targets(self) -> list[str]:
        """Return task IDs that were POSTed to /tasks/{id}/force-claim."""
        targets: list[str] = []
        for r in self.requests:
            if r.method != "POST":
                continue
            if "/force-claim" not in r.url.path:
                continue
            # URL path looks like /tasks/{id}/force-claim
            parts = r.url.path.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "tasks" and parts[2] == "force-claim":
                targets.append(parts[1])
        return targets


def _build_orchestrator(
    tmp_path: Path,
    recorder: _RequestRecorder | None = None,
) -> tuple[Orchestrator, _RequestRecorder]:
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
    rec = recorder or _RequestRecorder()
    transport = httpx.MockTransport(rec)
    client = httpx.Client(transport=transport, base_url="http://testserver")
    orch = Orchestrator(cfg, spawner, tmp_path, client=client)
    return orch, rec


# ---------------------------------------------------------------------------
# Tests: orphaned-claim detection
# ---------------------------------------------------------------------------


class TestFindOrphanedClaims:
    """Unit tests for WALRecovery.find_orphaned_claims (pure helper)."""

    def test_empty_wal_dir_returns_empty(self, tmp_path: Path) -> None:
        """No WAL directory -> no orphans."""
        orphans = WALRecovery.find_orphaned_claims(tmp_path / ".sdd")
        assert orphans == []

    def test_excludes_current_run(self, tmp_path: Path) -> None:
        """The in-progress run is not scanned for orphans."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="current", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)

        orphans = WALRecovery.find_orphaned_claims(sdd, exclude_run_id="current")
        assert orphans == []

    def test_claim_with_matching_spawn_confirmed_is_not_orphan(self, tmp_path: Path) -> None:
        """task_claimed paired with task_spawn_confirmed in the same run is committed work."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)
        w.append("task_spawn_confirmed", {"task_id": "T-1"}, {}, "lifecycle", committed=True)

        orphans = WALRecovery.find_orphaned_claims(sdd)
        assert orphans == []

    def test_claim_without_spawn_confirmed_is_orphan(self, tmp_path: Path) -> None:
        """task_claimed with no matching spawn_confirmed is flagged as orphan."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-2"}, {}, "lifecycle", committed=False)

        orphans = WALRecovery.find_orphaned_claims(sdd)
        assert len(orphans) == 1
        run_id, entry = orphans[0]
        assert run_id == "crashed-run"
        assert entry.inputs["task_id"] == "T-2"
        assert entry.decision_type == "task_claimed"

    def test_multiple_runs_mixed_state(self, tmp_path: Path) -> None:
        """Only claims without matching spawn_confirmed are flagged; across runs."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        ok = WALWriter(run_id="ok-run", sdd_dir=sdd)
        ok.append("task_claimed", {"task_id": "T-ok"}, {}, "lifecycle", committed=False)
        ok.append("task_spawn_confirmed", {"task_id": "T-ok"}, {}, "lifecycle", committed=True)

        bad = WALWriter(run_id="bad-run", sdd_dir=sdd)
        bad.append("task_claimed", {"task_id": "T-bad-1"}, {}, "lifecycle", committed=False)
        bad.append("task_claimed", {"task_id": "T-bad-2"}, {}, "lifecycle", committed=False)
        # T-bad-2 did get spawned, T-bad-1 did not
        bad.append("task_spawn_confirmed", {"task_id": "T-bad-2"}, {}, "lifecycle", committed=True)

        orphans = WALRecovery.find_orphaned_claims(sdd)
        task_ids = sorted(entry.inputs["task_id"] for _, entry in orphans)
        assert task_ids == ["T-bad-1"]


# ---------------------------------------------------------------------------
# Tests: end-to-end _recover_from_wal retry behavior
# ---------------------------------------------------------------------------


class TestRecoverFromWALRetriesOrphans:
    """Verify _recover_from_wal actually force-claims + writes task_retry."""

    def test_orphan_triggers_force_claim_post(self, tmp_path: Path) -> None:
        """An orphaned claim must POST /tasks/{id}/force-claim."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-abandoned"}, {}, "lifecycle", committed=False)

        orch, recorder = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        assert "T-abandoned" in recorder.force_claim_targets(), (
            f"Expected POST /tasks/T-abandoned/force-claim; recorded paths: {[str(r.url) for r in recorder.requests]}"
        )

    def test_non_orphan_does_not_trigger_force_claim(self, tmp_path: Path) -> None:
        """A task_claimed with matching task_spawn_confirmed must NOT be retried."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="ok-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-ok"}, {}, "lifecycle", committed=False)
        w.append("task_spawn_confirmed", {"task_id": "T-ok"}, {}, "lifecycle", committed=True)

        orch, recorder = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        assert recorder.force_claim_targets() == []

    def test_orphan_writes_task_retry_wal_entry(self, tmp_path: Path) -> None:
        """Recovery must append a committed task_retry entry per orphan."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-abandoned"}, {}, "lifecycle", committed=False)

        orch, _ = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        reader = WALReader(run_id=orch._run_id, sdd_dir=sdd)
        retry_entries = [e for e in reader.iter_entries() if e.decision_type == "task_retry"]
        assert len(retry_entries) == 1
        assert retry_entries[0].inputs["task_id"] == "T-abandoned"
        assert retry_entries[0].inputs["reason"] == "crash_recovery"
        assert retry_entries[0].inputs["original_run_id"] == "crashed-run"
        assert retry_entries[0].committed is True

    def test_ack_entry_marks_orphan_flag(self, tmp_path: Path) -> None:
        """wal_recovery_ack output.orphan is True for orphaned claims only."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="mixed-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-ok"}, {}, "lifecycle", committed=False)
        w.append("task_spawn_confirmed", {"task_id": "T-ok"}, {}, "lifecycle", committed=True)
        w.append("task_claimed", {"task_id": "T-orphan"}, {}, "lifecycle", committed=False)

        orch, _ = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        reader = WALReader(run_id=orch._run_id, sdd_dir=sdd)
        acks = [e for e in reader.iter_entries() if e.decision_type == "wal_recovery_ack"]
        orphan_flags = {e.inputs["original_inputs"]["task_id"]: e.output["orphan"] for e in acks}
        assert orphan_flags == {"T-ok": False, "T-orphan": True}

    def test_force_claim_failure_is_non_fatal(self, tmp_path: Path) -> None:
        """Server errors on force-claim must not break the recovery loop."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        w = WALWriter(run_id="crashed-run", sdd_dir=sdd)
        w.append("task_claimed", {"task_id": "T-1"}, {}, "lifecycle", committed=False)
        w.append("task_claimed", {"task_id": "T-2"}, {}, "lifecycle", committed=False)

        seen: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            if "/force-claim" in request.url.path:
                task_id = request.url.path.strip("/").split("/")[1]
                seen.append(task_id)
                # Fail the first request only
                if task_id == "T-1":
                    return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(_handler)
        client = httpx.Client(transport=transport, base_url="http://testserver")
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
        orch = Orchestrator(cfg, spawner, tmp_path, client=client)

        # Must not raise
        orch._recover_from_wal()

        # Both orphans must have been attempted
        assert sorted(seen) == ["T-1", "T-2"]

    def test_multiple_orphans_all_retried(self, tmp_path: Path) -> None:
        """Every orphaned claim across runs must be force-claimed."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        for i in range(3):
            w = WALWriter(run_id=f"crashed-{i}", sdd_dir=sdd)
            w.append("task_claimed", {"task_id": f"T-{i}"}, {}, "lifecycle", committed=False)

        orch, recorder = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        assert sorted(recorder.force_claim_targets()) == ["T-0", "T-1", "T-2"]


# ---------------------------------------------------------------------------
# Tests: worktree preservation
# ---------------------------------------------------------------------------


def _make_git_worktree(path: Path, *, dirty: bool = True) -> None:
    """Initialise a git repo at *path* and optionally leave a dirty file."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    # Need at least one commit so porcelain makes sense
    (path / ".gitkeep").write_text("")
    subprocess.run(["git", "add", ".gitkeep"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    if dirty:
        (path / "wip.txt").write_text("unsaved work")


class TestPreservePriorWorktrees:
    """Verify WIP in prior-run worktrees survives orchestrator startup.

    audit-088 changed the preservation mechanism: instead of renaming the
    worktree into ``.sdd/worktrees/preserved/``, :class:`AgentSpawner`
    runs ``cleanup_all_stale`` during init, which calls
    :func:`~bernstein.core.git.salvage.salvage_worktree` on every stale
    worktree.  That writes a patch + untracked-file dump to
    ``.sdd/runtime/salvage/<session>-<ts>/`` and (when the repo allows it)
    a ``salvage/<session>`` branch before the destructive worktree removal.

    ``_preserve_prior_worktrees_with_wip`` is now a no-op in the happy
    path because the stale worktree has already been salvaged + removed
    by the time it runs, but it remains wired as a safety net for paths
    that skip the spawner (e.g. ``use_worktrees=False`` deployments).
    """

    def _salvage_dir(self, tmp_path: Path) -> Path:
        return tmp_path / ".sdd" / "runtime" / "salvage"

    def test_dirty_worktree_moved_to_preserved(self, tmp_path: Path) -> None:
        """A prior worktree with uncommitted changes has its WIP salvaged.

        audit-088: "preserved" now means saved into
        ``.sdd/runtime/salvage/<session>-<ts>/`` (patch + untracked dump),
        not renamed into ``.sdd/worktrees/preserved/``.  The test name is
        kept for CI diff continuity.
        """
        worktree = tmp_path / ".sdd" / "worktrees" / "crashed-session-abc"
        _make_git_worktree(worktree, dirty=True)

        # Building the orchestrator triggers AgentSpawner init which calls
        # cleanup_all_stale → salvage_worktree on every stale worktree.
        _build_orchestrator(tmp_path)

        salvage_root = self._salvage_dir(tmp_path)
        assert salvage_root.exists(), "salvage directory should have been created"
        matches = sorted(salvage_root.glob("crashed-session-abc-*"))
        assert len(matches) == 1
        salvaged = matches[0]
        # The untracked wip.txt must have been captured (as untracked.json).
        # ``git worktree remove`` itself may fail in this synthetic test fixture
        # (the repo is a standalone init, not a linked worktree of a parent
        # repo) -- that's unrelated to the preservation guarantee, so we only
        # assert on the salvage artefacts that do reach the filesystem.
        assert (salvaged / "untracked.json").exists()
        assert (salvaged / "diff.patch").exists()

    def test_clean_worktree_is_not_salvaged(self, tmp_path: Path) -> None:
        """A clean worktree leaves no salvage trail."""
        worktree = tmp_path / ".sdd" / "worktrees" / "clean-session"
        _make_git_worktree(worktree, dirty=False)

        _build_orchestrator(tmp_path)

        salvage_root = self._salvage_dir(tmp_path)
        assert not any(salvage_root.glob("clean-session-*")) if salvage_root.exists() else True

    def test_preserve_helper_is_noop_after_salvage(self, tmp_path: Path) -> None:
        """The explicit preserve call is a no-op because salvage ran first.

        This locks in the audit-088 invariant: the salvage path runs before
        ``_preserve_prior_worktrees_with_wip``, so by the time the helper
        scans ``.sdd/worktrees/`` there is nothing left to move.
        """
        worktree = tmp_path / ".sdd" / "worktrees" / "crashed-session-xyz"
        _make_git_worktree(worktree, dirty=True)

        orch, _ = _build_orchestrator(tmp_path)
        preserved = orch._preserve_prior_worktrees_with_wip()
        assert preserved == []

    def test_preserved_root_itself_ignored(self, tmp_path: Path) -> None:
        """The .sdd/worktrees/preserved/ dir is skipped by the scan."""
        preserved_dir = tmp_path / ".sdd" / "worktrees" / "preserved"
        preserved_dir.mkdir(parents=True)
        (preserved_dir / "dummy").mkdir()

        orch, _ = _build_orchestrator(tmp_path)
        result = orch._preserve_prior_worktrees_with_wip()
        assert result == []

    def test_no_worktrees_dir_returns_empty(self, tmp_path: Path) -> None:
        """Missing .sdd/worktrees directory is a no-op."""
        orch, _ = _build_orchestrator(tmp_path)
        assert orch._preserve_prior_worktrees_with_wip() == []


# ---------------------------------------------------------------------------
# Regression: the pre-fix _recover_from_wal silently abandoned orphan claims.
# ---------------------------------------------------------------------------


class TestAudit001Regression:
    """Would-have-caught-the-bug scenario described in audit-001."""

    def test_crash_between_claim_and_spawn_does_not_silently_drop_task(self, tmp_path: Path) -> None:
        """End-to-end reproduction of the audit-001 work-loss scenario."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        # Prior run: 3 tasks, all claimed (committed=False), none spawned
        w = WALWriter(run_id="prior-run", sdd_dir=sdd)
        for tid in ("T-a", "T-b", "T-c"):
            w.append("task_claimed", {"task_id": tid}, {}, "lifecycle", committed=False)

        orch, recorder = _build_orchestrator(tmp_path)
        orch._recover_from_wal()

        # All three tasks must be re-queued
        assert sorted(recorder.force_claim_targets()) == ["T-a", "T-b", "T-c"]

        # And must have a task_retry WAL trail for auditability
        reader = WALReader(run_id=orch._run_id, sdd_dir=sdd)
        retries = [e for e in reader.iter_entries() if e.decision_type == "task_retry"]
        assert sorted(e.inputs["task_id"] for e in retries) == ["T-a", "T-b", "T-c"]
        assert all(e.inputs["reason"] == "crash_recovery" for e in retries)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-x", "-q"])


# Silence unused-import warning from narrow typing support
_ = Any
