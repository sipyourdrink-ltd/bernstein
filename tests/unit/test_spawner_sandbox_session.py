"""Unit tests for AgentSpawner sandbox-session routing (oai-002 phase 2).

Issue #2162 extends the seam with per-spawn sessions (backend + manifest
factory wiring), result sync-back, audit events, and the task-server
reachability probe; those paths are covered here with mocked sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from bernstein.core.models import AgentSession, ModelConfig
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.sandbox import WorkspaceManifest
from bernstein.core.sandbox.backend import ExecResult, SandboxSession
from bernstein.core.security.audit import AuditLog

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pytest


class _FakeAdapter(CLIAdapter):
    """Minimal adapter that records direct-spawn calls."""

    def __init__(self, adapter_name: str = "claude") -> None:
        super().__init__()
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
        del model_config, session_id, mcp_config, timeout_seconds
        del task_scope, budget_multiplier, system_addendum
        self.spawn_calls.append((prompt, workdir))
        return SpawnResult(pid=99, log_path=workdir / ".sdd" / "logs" / "direct.log")

    def is_alive(self, pid: int) -> bool:  # pragma: no cover - not used
        return pid == 99

    def kill(self, pid: int) -> None:  # pragma: no cover - not used
        del pid


class _FakeSession(SandboxSession):
    """In-memory :class:`SandboxSession` for spawner-routing tests."""

    def __init__(self, *, backend_name: str, root: Path) -> None:
        self.backend_name = backend_name
        self.session_id = "fake-sess"
        self.workdir = str(root)
        self._root = root
        self._exec_calls: list[list[str]] = []
        self._exec_blocker: asyncio.Event | None = None
        self.exit_code = 0

    @property
    def exec_calls(self) -> list[list[str]]:
        return self._exec_calls

    async def read(self, path: str) -> bytes:
        return (self._root / path).read_bytes()

    async def write(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        del mode
        target = self._root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        del cwd, env, timeout, stdin
        self._exec_calls.append(cmd.copy())
        if self._exec_blocker is not None:
            await self._exec_blocker.wait()
        return ExecResult(
            exit_code=self.exit_code,
            stdout=b"hello",
            stderr=b"",
            duration_seconds=0.01,
        )

    async def ls(self, path: str) -> list[str]:
        target = self._root / path
        return sorted(p.name for p in target.iterdir())

    async def snapshot(self) -> str:
        raise NotImplementedError

    async def shutdown(self) -> None:
        pass


def _build_spawner(tmp_path: Path, *, session: SandboxSession | None) -> tuple[AgentSpawner, _FakeAdapter]:
    adapter = _FakeAdapter("claude")
    with patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=False,
            sandbox_session=session,
        )
    return spawner, adapter


def test_spawn_via_sandbox_session_routes_through_session(tmp_path: Path) -> None:
    """A non-worktree session causes exec/file ops to run via the session."""
    session_obj = _FakeSession(backend_name="docker", root=tmp_path)
    spawner, adapter = _build_spawner(tmp_path, session=session_obj)
    agent_session = AgentSession(id="S-7", role="backend")

    result = spawner._spawn_via_sandbox_session(  # pyright: ignore[reportPrivateUsage]
        session_id="S-7",
        prompt="solve it",
        spawn_cwd=tmp_path,
        model_config=ModelConfig("sonnet", "high"),
        mcp_config=None,
        session=agent_session,
        adapter=adapter,
    )

    # Wait for the background thread's future to resolve.
    handle = spawner._sandbox_exec_handles["S-7"]  # pyright: ignore[reportPrivateUsage]
    handle.future.result(timeout=5.0)

    assert result.pid == 0
    assert agent_session.isolation == "container"
    assert agent_session.runtime_backend == "docker"
    assert adapter.spawn_calls == []
    # Prompt was written to the session-managed workdir.
    prompt_path = tmp_path / ".sdd" / "runtime" / "prompts" / "S-7.md"
    assert prompt_path.read_bytes() == b"solve it"
    # Adapter command was executed via session.exec at least once.
    assert session_obj.exec_calls, "session.exec was never invoked"


def test_worktree_session_does_not_trigger_routing(tmp_path: Path) -> None:
    """Worktree-backed sessions intentionally stay on the legacy path."""
    session_obj = _FakeSession(backend_name="worktree", root=tmp_path)
    spawner, _ = _build_spawner(tmp_path, session=session_obj)

    # Re-implement the dispatcher's gate inline so the test asserts the
    # exact predicate used in :meth:`AgentSpawner.spawn_for_tasks`.
    routes_through_session = (
        spawner.sandbox_session is not None
        and getattr(spawner.sandbox_session, "backend_name", "worktree") != "worktree"
    )
    assert routes_through_session is False
    # The session is still exposed for visibility, just not used for exec.
    assert spawner.sandbox_session is session_obj


def test_sandbox_session_check_alive_and_kill(tmp_path: Path) -> None:
    """Liveness and kill paths cooperate with the in-flight session future."""
    session_obj = _FakeSession(backend_name="docker", root=tmp_path)
    # Block exec until kill triggers cancellation, so the "still alive"
    # branch of _check_alive_sandbox_session is observable.
    block_loop = asyncio.new_event_loop()
    try:
        session_obj._exec_blocker = asyncio.Event()  # pyright: ignore[reportPrivateUsage]
    finally:
        block_loop.close()

    spawner, _ = _build_spawner(tmp_path, session=session_obj)
    agent_session = AgentSession(id="S-9", role="backend")

    spawner._spawn_via_sandbox_session(  # pyright: ignore[reportPrivateUsage]
        session_id="S-9",
        prompt="busy",
        spawn_cwd=tmp_path,
        model_config=ModelConfig("sonnet", "high"),
        mcp_config=None,
        session=agent_session,
        adapter=_FakeAdapter("claude"),
    )

    # Liveness should report True while the future has not resolved.
    assert spawner._check_alive_sandbox_session(agent_session) is True  # pyright: ignore[reportPrivateUsage]

    # Kill cancels the future and clears the handle.
    spawner._kill_local(agent_session)  # pyright: ignore[reportPrivateUsage]
    assert "S-9" not in spawner._sandbox_exec_handles  # pyright: ignore[reportPrivateUsage]
    assert agent_session.status == "dead"


# ---------------------------------------------------------------------------
# Issue #2162: per-spawn sessions, sync-back, audit events, reachability
# ---------------------------------------------------------------------------


class _FakeBackend:
    """In-memory SandboxBackend that mints one :class:`_FakeSession` per create."""

    name = "docker"
    capabilities: frozenset[object] = frozenset()

    def __init__(self, root: Path, *, block_exec: bool = False, exec_exit_code: int = 0) -> None:
        self._root = root
        self._block_exec = block_exec
        self._exec_exit_code = exec_exit_code
        self.created: list[_FakeSession] = []
        self.destroyed: list[str] = []

    async def create(self, manifest: object, options: dict[str, object] | None = None) -> _FakeSession:
        del manifest, options
        session_obj = _FakeSession(backend_name="docker", root=self._root)
        session_obj.session_id = f"sbx-{len(self.created)}"
        session_obj.exit_code = self._exec_exit_code
        if self._block_exec:
            session_obj._exec_blocker = asyncio.Event()  # pyright: ignore[reportPrivateUsage]
        self.created.append(session_obj)
        return session_obj

    async def resume(self, snapshot_id: str) -> _FakeSession:
        raise NotImplementedError

    async def destroy(self, session: SandboxSession) -> None:
        self.destroyed.append(session.session_id)
        await session.shutdown()


def _build_spawner_with_backend(
    tmp_path: Path,
    *,
    backend: _FakeBackend,
    server_port: int | None = None,
) -> tuple[AgentSpawner, _FakeAdapter]:
    adapter = _FakeAdapter("claude")
    with patch("bernstein.core.agents.spawner_core.get_registry", return_value=MagicMock()):
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_path,
            workdir=tmp_path,
            use_worktrees=False,
            sandbox_backend=backend,  # pyright: ignore[reportArgumentType]
            sandbox_manifest_factory=lambda: WorkspaceManifest(root=str(tmp_path)),
            sandbox_options={"image": "img:test"},
            sandbox_server_port=server_port,
        )
    return spawner, adapter


def _wait_until(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    """Poll *predicate* until truthy; exec-done callbacks run off-thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _spawn_one(spawner: AgentSpawner, adapter: _FakeAdapter, session_id: str) -> AgentSession:
    agent_session = AgentSession(id=session_id, role="backend")
    spawner._spawn_via_sandbox_session(  # pyright: ignore[reportPrivateUsage]
        session_id=session_id,
        prompt="do the thing",
        spawn_cwd=spawner._workdir,  # pyright: ignore[reportPrivateUsage]
        model_config=ModelConfig("sonnet", "high"),
        mcp_config=None,
        session=agent_session,
        adapter=adapter,
    )
    return agent_session


def test_backend_wiring_activates_session_routing(tmp_path: Path) -> None:
    """Backend + manifest factory activates the routing gate without a session."""
    backend = _FakeBackend(tmp_path)
    spawner, _ = _build_spawner_with_backend(tmp_path, backend=backend)
    assert spawner.sandbox_session is None
    assert spawner._sandbox_session_routing_active() is True  # pyright: ignore[reportPrivateUsage]


def test_per_spawn_session_provisioned_and_destroyed(tmp_path: Path) -> None:
    """Each spawn gets its own session; it is destroyed when the future resolves."""
    backend = _FakeBackend(tmp_path)
    spawner, adapter = _build_spawner_with_backend(tmp_path, backend=backend)

    agent_session = _spawn_one(spawner, adapter, "S-20")
    handle = spawner._sandbox_exec_handles["S-20"]  # pyright: ignore[reportPrivateUsage]
    handle.future.result(timeout=5.0)

    _wait_until(lambda: backend.destroyed)
    assert len(backend.created) == 1
    assert backend.destroyed == [backend.created[0].session_id]
    assert "S-20" not in spawner._sandbox_owned_sessions  # pyright: ignore[reportPrivateUsage]
    assert agent_session.runtime_backend == "docker"


def test_per_spawn_sessions_are_distinct_across_spawns(tmp_path: Path) -> None:
    """Two spawns never share a session (one container per agent)."""
    backend = _FakeBackend(tmp_path)
    spawner, adapter = _build_spawner_with_backend(tmp_path, backend=backend)

    _spawn_one(spawner, adapter, "S-21")
    _spawn_one(spawner, adapter, "S-22")
    for session_id in ("S-21", "S-22"):
        spawner._sandbox_exec_handles[session_id].future.result(timeout=5.0)  # pyright: ignore[reportPrivateUsage]

    _wait_until(lambda: len(backend.destroyed) == 2)
    assert len(backend.created) == 2
    assert backend.created[0].session_id != backend.created[1].session_id


def test_kill_destroys_per_spawn_session(tmp_path: Path) -> None:
    """kill() cancels the exec and tears down the agent's own session."""
    backend = _FakeBackend(tmp_path, block_exec=True)
    spawner, adapter = _build_spawner_with_backend(tmp_path, backend=backend)

    agent_session = _spawn_one(spawner, adapter, "S-23")
    assert spawner._check_alive_sandbox_session(agent_session) is True  # pyright: ignore[reportPrivateUsage]

    spawner._kill_local(agent_session)  # pyright: ignore[reportPrivateUsage]
    _wait_until(lambda: backend.destroyed)
    assert "S-23" not in spawner._sandbox_owned_sessions  # pyright: ignore[reportPrivateUsage]
    assert agent_session.status == "dead"


def test_sync_back_invoked_on_completion(tmp_path: Path) -> None:
    """The exec-done callback bundles the sandbox clone (best effort)."""
    backend = _FakeBackend(tmp_path)
    spawner, adapter = _build_spawner_with_backend(tmp_path, backend=backend)

    _spawn_one(spawner, adapter, "S-24")
    spawner._sandbox_exec_handles["S-24"].future.result(timeout=5.0)  # pyright: ignore[reportPrivateUsage]

    session_obj = backend.created[0]
    _wait_until(lambda: any(call[:3] == ["git", "bundle", "create"] for call in session_obj.exec_calls))
    bundle_calls = [call for call in session_obj.exec_calls if call[:3] == ["git", "bundle", "create"]]
    assert bundle_calls[0][3] == "/tmp/S-24.bundle"


def test_provisioning_failure_falls_back_to_direct_spawn(tmp_path: Path) -> None:
    """A backend that cannot create sessions falls back to adapter.spawn."""

    class _BrokenBackend(_FakeBackend):
        async def create(self, manifest: object, options: dict[str, object] | None = None) -> _FakeSession:
            raise RuntimeError("daemon went away")

    backend = _BrokenBackend(tmp_path)
    spawner, adapter = _build_spawner_with_backend(tmp_path, backend=backend)

    agent_session = AgentSession(id="S-25", role="backend")
    result = spawner._spawn_via_sandbox_session(  # pyright: ignore[reportPrivateUsage]
        session_id="S-25",
        prompt="fallback",
        spawn_cwd=tmp_path,
        model_config=ModelConfig("sonnet", "high"),
        mcp_config=None,
        session=agent_session,
        adapter=adapter,
    )
    assert result.pid == 99
    assert adapter.spawn_calls == [("fallback", tmp_path)]
    assert "S-25" not in spawner._sandbox_owned_sessions  # pyright: ignore[reportPrivateUsage]


def test_reachability_probe_warns_when_unreachable(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A failed in-container TCP probe logs a warning and never fails the spawn."""
    backend = _FakeBackend(tmp_path, exec_exit_code=1)
    spawner, adapter = _build_spawner_with_backend(tmp_path, backend=backend, server_port=8052)

    with caplog.at_level(logging.WARNING, logger="bernstein.core.agents.spawner_core"):
        _spawn_one(spawner, adapter, "S-26")
        spawner._sandbox_exec_handles["S-26"].future.result(timeout=5.0)  # pyright: ignore[reportPrivateUsage]

    assert any("cannot reach the task server" in record.getMessage() for record in caplog.records)
    # The probe itself was routed through the session.
    probe_calls = [call for call in backend.created[0].exec_calls if call[0] == "python3"]
    assert probe_calls and "socket.create_connection" in probe_calls[0][2]


def test_audit_events_appended_for_session_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifecycle events land in the HMAC-chained audit log."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    backend = _FakeBackend(tmp_path)
    spawner, adapter = _build_spawner_with_backend(tmp_path, backend=backend)

    _spawn_one(spawner, adapter, "S-27")
    spawner._sandbox_exec_handles["S-27"].future.result(timeout=5.0)  # pyright: ignore[reportPrivateUsage]

    audit_dir = tmp_path / ".sdd" / "audit"

    def _event_types() -> list[str]:
        events: list[str] = []
        for jsonl in audit_dir.glob("*.jsonl"):
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                events.append(json.loads(line)["event_type"])
        return events

    _wait_until(lambda: "sandbox.session_destroy" in _event_types())
    types = _event_types()
    for expected in (
        "sandbox.session_create",
        "sandbox.exec_start",
        "sandbox.exec_end",
        "sandbox.session_destroy",
    ):
        assert expected in types, f"missing audit event {expected} in {types}"

    valid, errors = AuditLog(audit_dir=audit_dir).verify()
    assert valid, f"audit chain not verifiable: {errors}"


def test_concurrent_lifecycles_keep_audit_chain_verifiable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent emissions from spawn + exec-done threads never fork the HMAC chain.

    Overlapping spawns emit session_create/exec_start from the spawning
    threads while exec_end/session_destroy fire from per-agent exec-done
    callback threads. Unserialized appends would recover the same chain
    tail twice and write sibling records, so verify() - not mere event
    presence - is the assertion that matters here.
    """
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    backend = _FakeBackend(tmp_path)
    spawner, adapter = _build_spawner_with_backend(tmp_path, backend=backend)

    session_ids = [f"S-CC-{i}" for i in range(4)]
    spawn_threads = [threading.Thread(target=_spawn_one, args=(spawner, adapter, sid)) for sid in session_ids]
    for thread in spawn_threads:
        thread.start()
    for thread in spawn_threads:
        thread.join(timeout=10.0)

    for sid in session_ids:
        spawner._sandbox_exec_handles[sid].future.result(timeout=5.0)  # pyright: ignore[reportPrivateUsage]

    audit_dir = tmp_path / ".sdd" / "audit"

    def _destroy_events() -> int:
        count = 0
        for jsonl in audit_dir.glob("*.jsonl"):
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                if json.loads(line)["event_type"] == "sandbox.session_destroy":
                    count += 1
        return count

    _wait_until(lambda: _destroy_events() >= len(session_ids))

    valid, errors = AuditLog(audit_dir=audit_dir).verify()
    assert valid, f"audit chain forked under concurrent emission: {errors}"
    assert errors == []


class _GitBundleSession(_FakeSession):
    """Fake session whose exec runs real git against a local clone.

    The sandbox-absolute ``/tmp/<sid>.bundle`` path is redirected into a
    per-test directory so the sync-back flow never touches the real /tmp.
    """

    def __init__(self, *, clone_root: Path, bundle_dir: Path) -> None:
        super().__init__(backend_name="docker", root=clone_root)
        self._clone_root = clone_root
        self._bundle_dir = bundle_dir

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        del cwd, env, timeout, stdin
        argv = [str(self._bundle_dir / Path(arg).name) if arg.startswith("/tmp/") else arg for arg in cmd]
        proc = subprocess.run(argv, cwd=self._clone_root, capture_output=True, check=False)
        return ExecResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, duration_seconds=0.0)

    async def read(self, path: str) -> bytes:
        return (self._bundle_dir / Path(path).name).read_bytes()


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_sync_back_fetches_bundle_refs_into_host_repo(tmp_path: Path) -> None:
    """Committed sandbox work becomes refs/remotes/sandbox/<sid>/* on the host."""
    host = tmp_path / "host"
    host.mkdir()
    _git(["init", "-q", "-b", "main"], host)
    (host / "a.txt").write_text("one")
    _git(["add", "."], host)
    _git(["commit", "-q", "-m", "init"], host)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(host), str(clone)], check=True, capture_output=True)
    _git(["checkout", "-q", "-b", "agent-work"], clone)
    (clone / "b.txt").write_text("two")
    _git(["add", "."], clone)
    _git(["commit", "-q", "-m", "agent commit"], clone)

    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    session_obj = _GitBundleSession(clone_root=clone, bundle_dir=bundle_dir)
    spawner, _ = _build_spawner(host, session=None)

    spawner._sync_back_sandbox_work(session_obj, "S-42")  # pyright: ignore[reportPrivateUsage]

    assert (host / ".sdd" / "runtime" / "sandbox" / "S-42.bundle").exists()
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/remotes/sandbox/S-42/"],
        cwd=host,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "refs/remotes/sandbox/S-42/agent-work" in refs
