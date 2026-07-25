"""An explicitly pinned container runtime must fail closed, whichever it is.

Regression coverage for issue #3039. ``--sandbox docker`` refused when the
runtime could not be provided, but ``--sandbox podman`` fell through to
worktree or host execution behind a log WARNING: both intent gates tested the
literal string ``"docker"`` instead of "the operator named a container
runtime". An explicit isolation request that silently yields no isolation is a
security defect, so every accepted container runtime is exercised here rather
than the one that happened to work.

The suite also pins the behaviour that must NOT change:

* ``worktree`` and the paid cloud backends keep their graceful fallback -
  they carry no container boundary for a provisioning failure to drop.
* A container request that was *not* pinned with ``--sandbox`` still degrades,
  surfaced and audited (issue #3014), rather than refusing.
* The legacy ``--container`` path's degrade to ``IsolationMode.NONE`` - the
  widest downgrade in the spawner - is recorded on that same surfaced-and-
  audited path instead of vanishing into a WARNING.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.container import ContainerError, ContainerHandle
from bernstein.core.models import AgentSession, IsolationMode, ModelConfig
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.sandbox import DockerSandbox
from bernstein.core.sandbox.explicit_attach import CONTAINER_SANDBOX_RUNTIMES
from bernstein.core.sandbox.selector import SandboxSelectionError

# Every runtime the ``--sandbox`` flag accepts that is NOT a container runtime.
# None of these may start refusing: worktree has no container boundary, and the
# cloud backends own their provisioning semantics.
NON_CONTAINER_SANDBOX_CHOICES = (
    "worktree",
    "e2b",
    "modal",
    "daytona",
    "blaxel",
    "runloop",
    "vercel",
    "microvm",
)


class FakeAdapter(CLIAdapter):
    """Adapter that records host spawns so a silent degrade is detectable."""

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

    def is_alive(self, pid: int) -> bool:  # pragma: no cover - not exercised
        return pid == 42

    def kill(self, pid: int) -> None:  # pragma: no cover - not exercised
        del pid


def _read_audit_events(audit_dir: Path) -> list[dict[str, Any]]:
    """Return every audit-chain entry under ``audit_dir`` as parsed dicts."""
    events: list[dict[str, Any]] = []
    if not audit_dir.exists():
        return events
    for jsonl in audit_dir.glob("*.jsonl"):
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            events.append(json.loads(line))
    return events


def _spawn_through_unavailable_runtime(
    tmp_path: Path,
    *,
    runtime: str,
    session_id: str,
) -> tuple[AgentSpawner, FakeAdapter, AgentSession, SpawnResult | None]:
    """Drive ``_spawn_in_sandbox`` with a runtime that cannot start a sandbox.

    Returns the spawner, the adapter (whose ``spawn_calls`` reveal a host
    fallback), the mutated session, and the result - ``None`` when the spawn
    refused.
    """
    adapter = FakeAdapter("codex")
    sandbox = DockerSandbox(enabled=True, runtime="podman" if runtime == "podman" else "docker")
    session = AgentSession(id=session_id, role="backend")

    with (
        patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()),
        patch(
            "bernstein.core.agents.spawner_core.spawn_in_sandbox",
            side_effect=ContainerError(f"Container runtime CLI '{runtime}' not found on PATH."),
        ),
    ):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=True,
            sandbox=sandbox,
        )
        try:
            result = spawner._spawn_in_sandbox(  # pyright: ignore[reportPrivateUsage]
                session_id=session_id,
                prompt="isolate me",
                spawn_cwd=tmp_path,
                model_config=ModelConfig("sonnet", "high"),
                mcp_config=None,
                session=session,
                adapter=adapter,
            )
        except SandboxSelectionError:
            raise
    return spawner, adapter, session, result


# ---------------------------------------------------------------------------
# The defect: an explicit container runtime that cannot be provided must refuse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runtime", sorted(CONTAINER_SANDBOX_RUNTIMES))
def test_explicit_container_runtime_refuses_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """Every pinned container runtime refuses; none degrades to host execution.

    Parametrised over the canonical runtime set rather than a hand-written
    list, so teaching the configuration about a new container runtime cannot
    reintroduce the gap without this test failing.
    """
    monkeypatch.setenv("BERNSTEIN_SANDBOX_RUNTIME", runtime)

    with pytest.raises(SandboxSelectionError) as excinfo:
        _spawn_through_unavailable_runtime(tmp_path, runtime=runtime, session_id=f"S-{runtime}")

    err = excinfo.value
    # The refusal names the runtime the operator actually pinned, with the
    # same shape as the historical docker refusal.
    assert err.attempted == (runtime,)
    assert f"--sandbox {runtime}" in err.reason
    assert "refusing to fall back" in err.reason.lower()


@pytest.mark.parametrize("runtime", sorted(CONTAINER_SANDBOX_RUNTIMES))
def test_explicit_container_runtime_never_runs_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """The refusal happens instead of the host spawn, not alongside it."""
    monkeypatch.setenv("BERNSTEIN_SANDBOX_RUNTIME", runtime)
    adapter = FakeAdapter("codex")
    sandbox = DockerSandbox(enabled=True, runtime="podman" if runtime == "podman" else "docker")
    session = AgentSession(id=f"S-host-{runtime}", role="backend")
    isolation_before = session.isolation

    with (
        patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()),
        patch(
            "bernstein.core.agents.spawner_core.spawn_in_sandbox",
            side_effect=ContainerError(f"Container runtime CLI '{runtime}' not found on PATH."),
        ),
    ):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=True,
            sandbox=sandbox,
        )
        with pytest.raises(SandboxSelectionError):
            spawner._spawn_in_sandbox(  # pyright: ignore[reportPrivateUsage]
                session_id=f"S-host-{runtime}",
                prompt="isolate me",
                spawn_cwd=tmp_path,
                model_config=ModelConfig("sonnet", "high"),
                mcp_config=None,
                session=session,
                adapter=adapter,
            )

    # The agent never ran anywhere: no host spawn, and the session's isolation
    # was not rewritten to a weaker boundary on the way out.
    assert adapter.spawn_calls == []
    assert session.isolation == isolation_before
    assert session.isolation != IsolationMode.WORKTREE.value
    assert session.isolation != IsolationMode.CONTAINER.value
    # A refusal is not a downgrade: nothing weaker was provided, so nothing is
    # recorded as such.
    assert spawner.isolation_downgrades == []


@pytest.mark.parametrize("runtime", sorted(CONTAINER_SANDBOX_RUNTIMES))
def test_explicit_container_runtime_is_recognised_as_operator_intent(
    monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """The intent signal reads "a container runtime was named", not "docker"."""
    monkeypatch.setenv("BERNSTEIN_SANDBOX_RUNTIME", runtime)
    assert AgentSpawner._sandbox_explicitly_requested() is True  # pyright: ignore[reportPrivateUsage]
    assert AgentSpawner._explicit_container_runtime() == runtime  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("runtime", sorted(CONTAINER_SANDBOX_RUNTIMES))
def test_explicit_container_runtime_refusal_survives_case_and_padding(
    monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """A pinned runtime is normalised before the gate reads it."""
    monkeypatch.setenv("BERNSTEIN_SANDBOX_RUNTIME", f"  {runtime.upper()} ")
    assert AgentSpawner._explicit_container_runtime() == runtime  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Non-regression: the backends that must keep degrading gracefully
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runtime", NON_CONTAINER_SANDBOX_CHOICES)
def test_non_container_sandbox_choice_is_not_operator_isolation_intent(
    monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """worktree and the paid cloud backends do not trip the container gate."""
    monkeypatch.setenv("BERNSTEIN_SANDBOX_RUNTIME", runtime)
    assert AgentSpawner._sandbox_explicitly_requested() is False  # pyright: ignore[reportPrivateUsage]
    assert AgentSpawner._explicit_container_runtime() is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("runtime", NON_CONTAINER_SANDBOX_CHOICES)
def test_non_container_sandbox_choice_still_degrades_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """A pinned non-container backend keeps the historical worktree fallback."""
    monkeypatch.setenv("BERNSTEIN_SANDBOX_RUNTIME", runtime)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))

    _spawner, adapter, session, result = _spawn_through_unavailable_runtime(
        tmp_path, runtime="docker", session_id=f"S-free-{runtime}"
    )

    assert result is not None
    assert result.pid == 42
    assert adapter.spawn_calls == [("isolate me", tmp_path)]
    assert session.isolation == IsolationMode.WORKTREE.value


def test_unpinned_container_request_still_degrades_and_is_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``--sandbox`` flag means the request degrades, surfaced and audited.

    The refusal is gated on operator intent, not on the runtime being absent,
    so a container request that came from ``sandbox:`` in bernstein.yaml keeps
    the graceful path from issue #3014.
    """
    monkeypatch.delenv("BERNSTEIN_SANDBOX_RUNTIME", raising=False)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))

    spawner, adapter, session, result = _spawn_through_unavailable_runtime(
        tmp_path, runtime="podman", session_id="S-unpinned"
    )

    assert result is not None
    assert adapter.spawn_calls == [("isolate me", tmp_path)]
    assert session.isolation == IsolationMode.WORKTREE.value
    downgrades = spawner.isolation_downgrades
    assert len(downgrades) == 1
    assert downgrades[0].requested == IsolationMode.CONTAINER.value
    assert downgrades[0].actual == IsolationMode.WORKTREE.value


# ---------------------------------------------------------------------------
# The legacy ``--container`` path's degrade to NONE
# ---------------------------------------------------------------------------


def _spawner_with_failing_container_manager(tmp_path: Path, adapter: FakeAdapter, *, error: str) -> AgentSpawner:
    """Build a spawner whose container manager cannot spawn.

    The manager is injected rather than constructed so the test needs no
    container runtime CLI on PATH.
    """
    with patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=False,
        )
    manager = MagicMock()
    manager.config.two_phase_sandbox = None
    manager.spawn_in_container.side_effect = ContainerError(error)
    spawner._container_mgr = manager  # pyright: ignore[reportPrivateUsage]
    return spawner


def test_legacy_container_degrade_to_none_is_surfaced_and_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--container`` losing its container records a container -> none downgrade.

    This is the widest downgrade the spawner can make: the agent ends up on the
    host with no boundary at all. It used to sit outside the surfaced-and-
    audited path, so the operator saw only a WARNING.
    """
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.delenv("BERNSTEIN_SANDBOX_RUNTIME", raising=False)

    adapter = FakeAdapter("claude")
    spawner = _spawner_with_failing_container_manager(tmp_path, adapter, error="Cannot connect to the Docker daemon.")
    session = AgentSession(id="S-legacy", role="backend")

    result = spawner._spawn_in_container(  # pyright: ignore[reportPrivateUsage]
        session_id="S-legacy",
        prompt="legacy container",
        spawn_cwd=tmp_path,
        model_config=ModelConfig("sonnet", "high"),
        mcp_config=None,
        session=session,
        adapter=adapter,
    )

    # Behaviour preserved: the run continues on the host.
    assert result.pid == 42
    assert session.isolation == IsolationMode.NONE.value

    # 1) Surfaced on the spawner so the run summary can render it.
    downgrades = spawner.isolation_downgrades
    assert len(downgrades) == 1
    entry = downgrades[0]
    assert entry.session_id == "S-legacy"
    assert entry.requested == IsolationMode.CONTAINER.value
    assert entry.actual == IsolationMode.NONE.value
    assert "docker daemon" in entry.reason.lower()

    # 2) Audited in the HMAC-chained log with requested-vs-actual isolation.
    downgrade_events = [
        e for e in _read_audit_events(tmp_path / ".sdd" / "audit") if e["event_type"] == "sandbox.isolation_downgrade"
    ]
    assert len(downgrade_events) == 1
    details = downgrade_events[0]["details"]
    assert isinstance(details, dict)
    assert details["requested_isolation"] == IsolationMode.CONTAINER.value
    assert details["actual_isolation"] == IsolationMode.NONE.value


def test_legacy_container_success_records_no_downgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A container that starts records nothing: the surface stays un-noised."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.delenv("BERNSTEIN_SANDBOX_RUNTIME", raising=False)

    adapter = FakeAdapter("claude")
    with patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=False,
        )
    manager = MagicMock()
    manager.config.two_phase_sandbox = None
    manager.spawn_in_container.return_value = ContainerHandle(container_id="c-ok", session_id="S-ok", pid=321)
    spawner._container_mgr = manager  # pyright: ignore[reportPrivateUsage]
    session = AgentSession(id="S-ok", role="backend")

    result = spawner._spawn_in_container(  # pyright: ignore[reportPrivateUsage]
        session_id="S-ok",
        prompt="legacy container",
        spawn_cwd=tmp_path,
        model_config=ModelConfig("sonnet", "high"),
        mcp_config=None,
        session=session,
        adapter=adapter,
    )

    assert result.pid == 321
    assert session.isolation == IsolationMode.CONTAINER.value
    assert adapter.spawn_calls == []
    assert spawner.isolation_downgrades == []
