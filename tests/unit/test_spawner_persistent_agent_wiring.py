"""Verify ``record_persistent_agent_step`` is wired into both spawn paths.

Tests confirm that the journal event is emitted through the real spawn
flow (not just the unit-tested helper), and that the evidence-bundle
verdict reflects the presence of the persistent-agent event.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock as unittest_mock

from bernstein.adapters._contract import AdapterStrategy, SessionState
from bernstein.core.defaults import JOURNAL_EVENT_PERSISTENT_AGENT_STEP
from bernstein.core.evidence.run_artifacts import (
    ArtifactPayload,
    post_run_artifact,
    verify_run_artifacts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY = b"agent-step-wiring-test-hmac-key-012345"


def _sdd(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    sdd.mkdir(exist_ok=True)
    return sdd


# ---------------------------------------------------------------------------
# Primary spawn path (spawn_for_tasks)
# ---------------------------------------------------------------------------


class TestPrimarySpawnWiring:
    """Wiring in ``_spawn_for_tasks_internal`` — the main spawn path."""

    def test_persistent_adapter_records_step(
        self,
        tmp_path: Path,
        make_task,
        mock_adapter_factory,
    ) -> None:
        """A persistent-agent adapter creates a ``persistent_agent_step`` journal event."""
        adapter = mock_adapter_factory(pid=100)
        adapter.name.return_value = "persistent-test"

        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        from bernstein.core.spawner import AgentSpawner

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            default_model="mock-model",
        )

        task = make_task()
        with unittest_mock.patch(
            "bernstein.adapters._contract.STRATEGY_MATRIX",
            {"persistent-test": AdapterStrategy(session_state=SessionState.PERSISTENT_AGENT)},
        ):
            session = spawner.spawn_for_tasks([task])

        assert session.task_ids == ["T-001"]

        from bernstein.core.replay.journal import load_events

        journal_path = tmp_path / ".sdd" / "runs" / "task-T-001" / "journal.jsonl"
        assert journal_path.is_file(), "persistent-agent adapter must create a journal"
        events = load_events(journal_path).events
        step_events = [
            r for r in events if str(r.get("event", "")) == JOURNAL_EVENT_PERSISTENT_AGENT_STEP
        ]
        assert len(step_events) == 1
        assert step_events[0]["task_id"] == "T-001"
        assert step_events[0]["adapter"] == "persistent-test"

    def test_stateless_adapter_records_no_step(
        self,
        tmp_path: Path,
        make_task,
        mock_adapter_factory,
    ) -> None:
        """A stateless adapter must not create a journal — runs stay byte-identical."""
        adapter = mock_adapter_factory(pid=100)

        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        from bernstein.core.spawner import AgentSpawner

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            default_model="mock-model",
        )

        task = make_task()
        session = spawner.spawn_for_tasks([task])

        assert session.task_ids == ["T-001"]

        journal_path = tmp_path / ".sdd" / "runs" / "task-T-001" / "journal.jsonl"
        assert not journal_path.is_file(), "stateless adapter must not create a journal"

    def test_persistent_step_forces_unverifiable_identity(
        self,
        tmp_path: Path,
        make_task,
        mock_adapter_factory,
    ) -> None:
        """After a persistent-agent spawn, ``verify_run_artifacts`` reports unverifiable."""
        adapter = mock_adapter_factory(pid=100)
        adapter.name.return_value = "persistent-test"

        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        from bernstein.core.spawner import AgentSpawner

        spawner = AgentSpawner(
            adapter,
            templates_dir,
            tmp_path,
            use_worktrees=False,
            default_model="mock-model",
        )

        task = make_task()
        with unittest_mock.patch(
            "bernstein.adapters._contract.STRATEGY_MATRIX",
            {"persistent-test": AdapterStrategy(session_state=SessionState.PERSISTENT_AGENT)},
        ):
            spawner.spawn_for_tasks([task])

        sdd = _sdd(tmp_path)
        post_run_artifact(
            sdd_dir=sdd,
            task_id="T-001",
            key="report",
            payload=ArtifactPayload.report("# Wiring Test"),
            actor="worker",
            hmac_key=_KEY,
        )

        results = verify_run_artifacts(sdd, "T-001", hmac_key=_KEY)
        assert len(results) == 1
        assert results[0].ok, results[0].reason
        assert results[0].journal_identity == "unverifiable"


# ---------------------------------------------------------------------------
# Crash-resume spawn path (spawn_for_resume)
# ---------------------------------------------------------------------------


class TestResumeSpawnWiring:
    """Wiring in ``spawn_for_resume`` — the crash-recovery path."""

    def test_persistent_adapter_records_step_on_resume(
        self,
        tmp_path: Path,
    ) -> None:
        """A persistent-agent adapter records the journal event on resume spawn."""
        from unittest.mock import MagicMock

        from bernstein.core.models import Task
        from bernstein.core.spawner import AgentSpawner

        from bernstein.adapters.base import CLIAdapter, SpawnResult

        adapter = MagicMock(spec=CLIAdapter)
        adapter.spawn.return_value = SpawnResult(pid=200, proc=None, log_path=None)
        adapter.is_alive.return_value = True
        adapter.is_rate_limited.return_value = False
        adapter.name.return_value = "persistent-test"

        spawner = AgentSpawner(
            adapter,
            tmp_path / "templates",
            tmp_path,
            default_model="mock-model",
        )

        worktree_path = tmp_path / ".sdd" / "worktrees" / "old-session"
        worktree_path.mkdir(parents=True)

        task = Task(
            id="T-R01",
            title="Resume task",
            description="Continue.",
            role="backend",
        )

        with unittest_mock.patch(
            "bernstein.adapters._contract.STRATEGY_MATRIX",
            {"persistent-test": AdapterStrategy(session_state=SessionState.PERSISTENT_AGENT)},
        ):
            session = spawner.spawn_for_resume(
                [task], worktree_path=worktree_path, changed_files=[]
            )

        assert session.task_ids == ["T-R01"]

        from bernstein.core.replay.journal import load_events

        journal_path = tmp_path / ".sdd" / "runs" / "task-T-R01" / "journal.jsonl"
        assert journal_path.is_file(), "persistent-agent resume must create a journal"
        events = load_events(journal_path).events
        step_events = [
            r for r in events if str(r.get("event", "")) == JOURNAL_EVENT_PERSISTENT_AGENT_STEP
        ]
        assert len(step_events) == 1
        assert step_events[0]["task_id"] == "T-R01"
        assert step_events[0]["adapter"] == "persistent-test"

    def test_stateless_adapter_records_no_step_on_resume(
        self,
        tmp_path: Path,
    ) -> None:
        """A stateless adapter must not create a journal on resume — byte-identical."""
        from unittest.mock import MagicMock

        from bernstein.core.models import Task
        from bernstein.core.spawner import AgentSpawner

        from bernstein.adapters.base import CLIAdapter, SpawnResult

        adapter = MagicMock(spec=CLIAdapter)
        adapter.spawn.return_value = SpawnResult(pid=200, proc=None, log_path=None)
        adapter.is_alive.return_value = True
        adapter.is_rate_limited.return_value = False

        spawner = AgentSpawner(
            adapter,
            tmp_path / "templates",
            tmp_path,
            default_model="mock-model",
        )

        worktree_path = tmp_path / ".sdd" / "worktrees" / "old-session"
        worktree_path.mkdir(parents=True)

        task = Task(
            id="T-R02",
            title="Resume task",
            description="Continue.",
            role="backend",
        )

        session = spawner.spawn_for_resume(
            [task], worktree_path=worktree_path, changed_files=[]
        )

        assert session.task_ids == ["T-R02"]

        journal_path = tmp_path / ".sdd" / "runs" / "task-T-R02" / "journal.jsonl"
        assert not journal_path.is_file(), "stateless adapter must not create a journal on resume"
