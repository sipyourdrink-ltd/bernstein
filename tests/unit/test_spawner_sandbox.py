"""Unit tests for AgentSpawner sandbox wiring."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.container import ContainerError, ContainerHandle
from bernstein.core.models import AgentSession, IsolationMode, ModelConfig
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.sandbox import DockerSandbox


class FakeAdapter(CLIAdapter):
    """Minimal adapter used to test spawner sandbox paths."""

    def __init__(self, adapter_name: str = "claude") -> None:
        self._name = adapter_name
        self.spawn_calls: list[tuple[str, Path]] = []

    def name(self) -> str:
        return self._name

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, object] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        del model_config, session_id, mcp_config, timeout_seconds, task_scope, budget_multiplier, system_addendum
        self.spawn_calls.append((prompt, workdir))
        return SpawnResult(pid=42, log_path=workdir / ".sdd" / "logs" / "fallback.log")

    def is_alive(self, pid: int) -> bool:  # pragma: no cover - not used here
        return pid == 42

    def kill(self, pid: int) -> None:  # pragma: no cover - not used here
        del pid


def test_spawn_in_sandbox_uses_sandbox_path(tmp_path: Path) -> None:
    """Spawner should use the sandbox helper when configured."""

    adapter = FakeAdapter("claude")
    sandbox = DockerSandbox(enabled=True, adapter_images={"claude": "bernstein/claude:latest"})
    session = AgentSession(id="S-1", role="backend")
    fake_handle = ContainerHandle(container_id="sandbox-1", session_id="S-1", pid=222)

    with (
        patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()),
        patch(
            "bernstein.core.agents.spawner_core.spawn_in_sandbox", return_value=(MagicMock(), fake_handle)
        ) as sandbox_spawn,
    ):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=False,
            sandbox=sandbox,
        )
        result = spawner._spawn_in_sandbox(  # pyright: ignore[reportPrivateUsage]
            session_id="S-1",
            prompt="solve it",
            spawn_cwd=tmp_path,
            model_config=ModelConfig("sonnet", "high"),
            mcp_config=None,
            session=session,
            adapter=adapter,
        )

    assert result.pid == 222
    assert session.container_id == "sandbox-1"
    assert session.isolation == "container"
    assert adapter.spawn_calls == []
    assert sandbox_spawn.call_args.kwargs["adapter_name"] == "claude"


def test_spawn_in_sandbox_falls_back_to_adapter_on_runtime_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime setup errors should fall back to the normal worktree adapter path."""

    # The fallback is for a container request the operator did not pin with
    # ``--sandbox``; a pinned container runtime refuses instead (issue #3039).
    monkeypatch.delenv("BERNSTEIN_SANDBOX_RUNTIME", raising=False)
    adapter = FakeAdapter("codex")
    sandbox = DockerSandbox(enabled=True)
    session = AgentSession(id="S-2", role="backend")

    with (
        patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()),
        patch("bernstein.core.agents.spawner_core.spawn_in_sandbox", side_effect=ContainerError("docker unavailable")),
    ):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=True,
            sandbox=sandbox,
        )
        result = spawner._spawn_in_sandbox(  # pyright: ignore[reportPrivateUsage]
            session_id="S-2",
            prompt="fallback",
            spawn_cwd=tmp_path,
            model_config=ModelConfig("sonnet", "high"),
            mcp_config=None,
            session=session,
            adapter=adapter,
        )

    assert result.pid == 42
    assert adapter.spawn_calls == [("fallback", tmp_path)]
    assert session.container_id is None
    assert session.isolation == "worktree"


def _read_audit_events(audit_dir: Path) -> list[dict[str, object]]:
    """Return every audit-chain entry under ``audit_dir`` as parsed dicts."""
    events: list[dict[str, object]] = []
    for jsonl in audit_dir.glob("*.jsonl"):
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            events.append(json.loads(line))
    return events


def test_non_explicit_docker_request_downgrade_is_surfaced_and_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #3014: a non-explicit docker isolation request that cannot be

    honoured (no container runtime) must record a *surfaced* downgrade the run
    summary can render AND an audit-chain entry noting requested-vs-actual
    isolation - not just a log WARNING.

    "Non-explicit" here means the sandbox came from the ``sandbox:`` section of
    bernstein.yaml rather than from the ``--sandbox`` flag, so the
    ``BERNSTEIN_SANDBOX_RUNTIME`` intent signal is unset and the spawn degrades
    gracefully instead of refusing (the explicit path refuses - issue #2809).
    This is the path the published image takes.
    """
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    # Configured via `sandbox:`, NOT pinned by --sandbox: the intent signal is
    # unset, so this exercises the graceful-degradation path.
    monkeypatch.delenv("BERNSTEIN_SANDBOX_RUNTIME", raising=False)

    adapter = FakeAdapter("codex")
    sandbox = DockerSandbox(enabled=True)
    session = AgentSession(id="S-3014", role="backend")

    with (
        patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()),
        patch(
            "bernstein.core.agents.spawner_core.spawn_in_sandbox",
            side_effect=ContainerError("Container runtime CLI 'docker' not found on PATH."),
        ),
    ):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=True,
            sandbox=sandbox,
        )
        result = spawner._spawn_in_sandbox(  # pyright: ignore[reportPrivateUsage]
            session_id="S-3014",
            prompt="fallback",
            spawn_cwd=tmp_path,
            model_config=ModelConfig("sonnet", "high"),
            mcp_config=None,
            session=session,
            adapter=adapter,
        )

    # The graceful fallback still runs on worktree isolation (behaviour kept).
    assert result.pid == 42
    assert session.isolation == IsolationMode.WORKTREE.value

    # 1) Surfaced on the spawner so the run summary can render requested-vs-actual.
    downgrades = spawner.isolation_downgrades
    assert len(downgrades) == 1
    entry = downgrades[0]
    assert entry.session_id == "S-3014"
    assert entry.requested == IsolationMode.CONTAINER.value
    assert entry.actual == IsolationMode.WORKTREE.value
    assert "docker" in entry.reason.lower()

    # 2) Audited in the HMAC-chained audit log with requested-vs-actual isolation.
    audit_dir = tmp_path / ".sdd" / "audit"
    downgrade_events = [e for e in _read_audit_events(audit_dir) if e["event_type"] == "sandbox.isolation_downgrade"]
    assert len(downgrade_events) == 1
    details = downgrade_events[0]["details"]
    assert isinstance(details, dict)
    assert details["requested_isolation"] == IsolationMode.CONTAINER.value
    assert details["actual_isolation"] == IsolationMode.WORKTREE.value

    # 3) The run-summary surface renders requested-vs-actual isolation.
    from bernstein.cli.display.summary_card import RunSummaryData, write_summary_json

    data = RunSummaryData(
        run_id="R-3014",
        tasks_completed=1,
        tasks_total=1,
        tasks_failed=0,
        wall_clock_seconds=1.0,
        total_cost_usd=0.0,
        quality_score=None,
        isolation_downgrades=[d.as_dict() for d in downgrades],
    )
    summary_path = write_summary_json(data, "R-3014", tmp_path / ".sdd")
    summary_text = summary_path.read_text(encoding="utf-8")
    assert "isolation" in summary_text.lower()
    assert IsolationMode.CONTAINER.value in summary_text
    assert IsolationMode.WORKTREE.value in summary_text


def test_worktree_only_spawner_records_no_downgrade(tmp_path: Path) -> None:
    """Operator explicitly choosing worktree isolation is left un-noised: no

    downgrade is recorded and no ``sandbox.isolation_downgrade`` audit event is
    emitted when no stronger isolation was ever requested (issue #3014).
    """
    adapter = FakeAdapter("claude")

    with patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=True,
            sandbox=None,
        )

    assert spawner.isolation_downgrades == []
    audit_dir = tmp_path / ".sdd" / "audit"
    if audit_dir.exists():
        assert not [e for e in _read_audit_events(audit_dir) if e["event_type"] == "sandbox.isolation_downgrade"]
