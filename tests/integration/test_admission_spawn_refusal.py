"""End-to-end spawn-gate tests for the executor admission policy (#4907).

These drive the real :meth:`AgentSpawner.spawn_for_tasks` path rather
than :meth:`AdmissionPolicy.evaluate`, to prove the policy is wired in
ahead of process start: a refused spawn starts no agent, reaches a
``SpawnError``, leaves a decision record naming the deciding rule, and
appends an ``admission_refusal`` event to the HMAC-chained audit log.

The policy is declared in the workdir's own ``bernstein.yaml``, which is
where an operator declares it, so the tests also cover the file-loading
seam.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnError, SpawnResult
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.security.audit import AuditLog

_ADMISSION_RECORD_REL = Path(".sdd") / "runtime" / "spawn_admission"


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    """Per-test workdir: a git repo plus the minimal role template tree."""
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "ci@example.com"],
        ["git", "config", "user.name", "ci"],
    ):
        subprocess.run(args, cwd=str(tmp_path), check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# admission integration test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(tmp_path), check=True, capture_output=True)

    role_dir = tmp_path / "templates" / "roles" / "backend"
    role_dir.mkdir(parents=True)
    (role_dir / "system_prompt.md").write_text("You are a backend specialist.", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def mock_adapter() -> MagicMock:
    """Mock :class:`CLIAdapter` that reports a successful spawn."""
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=4242, log_path=Path("/tmp/admission-test.log"))
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    adapter.name.return_value = "mockcli"
    return adapter


@pytest.fixture()
def make_task() -> Iterator[Any]:  # type: ignore[misc]
    """Yield a factory that builds a minimal :class:`Task`."""
    from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

    def _factory(role: str = "backend") -> Task:
        return Task(
            id="T-001",
            title="Admission integration task",
            description="Drive the spawner through the admission gate.",
            role=role,
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
            priority=2,
        )

    yield _factory


def _write_admission(workdir: Path, block: str) -> None:
    """Write a ``bernstein.yaml`` carrying the given ``admission:`` block."""
    (workdir / "bernstein.yaml").write_text(f"goal: integration test\n{block}", encoding="utf-8")


def _build_spawner(workdir: Path, adapter: MagicMock) -> AgentSpawner:
    """Construct an :class:`AgentSpawner` plumbed like production."""
    return AgentSpawner(
        adapter=adapter,
        templates_dir=workdir / "templates" / "roles",
        workdir=workdir,
        use_worktrees=False,
        default_model="mock-model",
    )


def _record(workdir: Path, session_id: str) -> dict[str, Any]:
    """Read back the persisted admission decision record for a session."""
    path = workdir / _ADMISSION_RECORD_REL / f"{session_id}.json"
    assert path.exists(), f"Admission decision record missing at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


class TestRefusal:
    """A subject no allow rule admits must never reach the adapter."""

    def test_denied_spawn_starts_no_agent_process(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n  rules:\n    - id: approved-adapters\n      effect: allow\n      adapters: [codex]\n",
        )
        calls: list[Any] = []
        real_popen = subprocess.Popen
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda *a, **k: (calls.append(a), real_popen(*a, **k))[1],  # type: ignore[misc]
        )
        spawner = _build_spawner(workdir, mock_adapter)

        with pytest.raises(SpawnError, match="admission"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()
        assert calls == [], "A refused spawn must not start any subprocess."

    def test_denied_spawn_records_the_rule_that_refused_it(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n"
            "  rules:\n"
            "    - id: no-unsandboxed\n"
            "      effect: deny\n"
            "      sandboxes: [none]\n"
            "    - id: approved-adapters\n"
            "      effect: allow\n"
            "      adapters: [mockcli]\n",
        )
        spawner = _build_spawner(workdir, mock_adapter)

        with pytest.raises(SpawnError):
            spawner.spawn_for_tasks([make_task()])

        records = sorted((workdir / _ADMISSION_RECORD_REL).glob("*.json"))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding="utf-8"))
        assert record["allowed"] is False
        assert record["rule_id"] == "no-unsandboxed"
        assert record["effect"] == "deny"
        assert record["subject"]["adapter"] == "mockcli"
        assert record["subject"]["role"] == "backend"

    def test_denied_spawn_emits_admission_refusal_audit_event(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n  rules:\n    - id: approved-adapters\n      effect: allow\n      adapters: [codex]\n",
        )
        spawner = _build_spawner(workdir, mock_adapter)

        with pytest.raises(SpawnError):
            spawner.spawn_for_tasks([make_task()])

        audit = AuditLog(audit_dir=workdir / ".sdd" / "audit")
        ok, errors = audit.verify()
        assert ok, f"Audit chain integrity broke: {errors}"
        events = list(audit.query(event_type="admission_refusal"))
        assert events, "A refused spawn must leave a verifiable audit-chain event."
        first = events[0]
        assert first.actor == "spawner"
        assert first.details.get("rule_id") == ""
        assert first.details.get("subject", {}).get("adapter") == "mockcli"

    def test_malformed_policy_refuses_rather_than_admitting(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n  rules:\n    - id: broken\n      effect: permit\n      adapters: [mockcli]\n",
        )
        spawner = _build_spawner(workdir, mock_adapter)

        with pytest.raises(SpawnError, match="admission"):
            spawner.spawn_for_tasks([make_task()])

        mock_adapter.spawn.assert_not_called()


class TestAdmission:
    """An admitted spawn proceeds and records the rule that admitted it."""

    def test_admitted_spawn_records_the_rule_id_that_admitted_it(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n"
            "  rules:\n"
            "    - id: approved-adapters\n"
            "      effect: allow\n"
            "      adapters: [mockcli]\n"
            "      models: ['mock-*']\n",
        )
        spawner = _build_spawner(workdir, mock_adapter)

        session = spawner.spawn_for_tasks([make_task()])

        assert session.pid == 4242
        record = _record(workdir, session.id)
        assert record["allowed"] is True
        assert record["rule_id"] == "approved-adapters"
        assert record["subject"]["model"] == "mock-model"

    def test_warn_mode_admits_and_records_the_refusing_rule(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        _write_admission(
            workdir,
            "admission:\n"
            "  mode: warn\n"
            "  rules:\n"
            "    - id: approved-adapters\n"
            "      effect: allow\n"
            "      adapters: [codex]\n",
        )
        spawner = _build_spawner(workdir, mock_adapter)

        session = spawner.spawn_for_tasks([make_task()])

        record = _record(workdir, session.id)
        assert record["allowed"] is True
        assert record["rule_id"] == ""
        assert record["mode"] == "warn"


class TestNoPolicyDeclared:
    """A repository that declares no policy is unaffected."""

    def test_absent_admission_block_leaves_the_spawn_unchanged(
        self,
        workdir: Path,
        mock_adapter: MagicMock,
        make_task: Any,
    ) -> None:
        (workdir / "bernstein.yaml").write_text("goal: integration test\n", encoding="utf-8")
        spawner = _build_spawner(workdir, mock_adapter)

        session = spawner.spawn_for_tasks([make_task()])

        assert session.pid == 4242
        assert not (workdir / _ADMISSION_RECORD_REL).exists()
