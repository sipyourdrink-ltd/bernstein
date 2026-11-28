"""``PreviewManager`` - orchestrates a single ``bernstein preview`` lifecycle.

Responsibilities:

* Resolve a runnable command (auto-discovery + ``--command`` override).
* Spawn the dev server inside a :class:`SandboxBackend` session, reusing
  the most recent worktree when one exists.
* Stream stdout to capture the bound port via
  :func:`~bernstein.core.preview.port_capture.capture_port`, then verify
  it with :func:`probe_port`.
* Open a public tunnel through :class:`TunnelBridge`, mint a
  short-lived auth credential via :class:`PreviewTokenIssuer`, and
  persist a :class:`PreviewState` record to ``.sdd/runtime/preview/state.json``.
* On any failure roll the entire stack back: tunnel destroyed, sandbox
  process killed, preview record removed.

Every state-changing transition emits an HMAC-chained audit entry. The
manager is deterministic: identical inputs produce identical preview
ids, and tests can swap every collaborator (sandbox, tunnel, audit log,
clock).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from bernstein.core.preview.command_discovery import (
    DiscoveredCommand,
    discover_commands,
    list_candidates,
)
from bernstein.core.preview.metrics import (
    record_link_issued,
    record_preview_started,
    record_preview_stopped,
)
from bernstein.core.preview.port_capture import (
    PortNotDetectedError,
    capture_port,
    probe_port,
)
from bernstein.core.preview.token_issuer import IssuedAuth, PreviewTokenIssuer
from bernstein.core.preview.tunnel_bridge import TunnelBridge, TunnelBridgeError
from bernstein.core.sandbox.isolated_process import IsolationUnavailable, readonly_bind, runtime_executable
from bernstein.core.security.audit import AuditLog

if TYPE_CHECKING:
    import builtins

logger = logging.getLogger(__name__)


PREVIEW_STATE_DIR = Path(".sdd/runtime/preview")
PREVIEW_STATE_FILE = PREVIEW_STATE_DIR / "state.json"
DEFAULT_AUDIT_DIR = Path(".sdd/audit")

DEFAULT_EXPIRE_SECONDS = 4 * 3600  # 4 hours, per the ticket.

#: Maximum difference allowed between the spawn-time and teardown-time
#: ``psutil.Process.create_time()`` readings for the same process on non-Linux
#: platforms (or legacy Linux state lacking ``process_start_ticks``).
#: Deliberately tight and fail-closed: long-lived in-process callers on macOS
#: may see a larger shift after host sleep adjusts ``kern.boottime`` and will
#: skip signaling rather than risk signaling a recycled PID. On Linux,
#: :attr:`PreviewState.process_start_ticks` binds directly to immutable
#: ``/proc/<pid>/stat`` field 22 ticks so ``/proc/stat`` ``btime`` integer-second
#: NTP adjustments cannot cause false mismatches.
_PROCESS_START_TIME_TOLERANCE_SECONDS = 0.05

#: Root of Linux procfs; overridable in tests so host ``/proc`` is never probed.
_PROC_ROOT = Path("/proc")

#: Map of duration suffixes recognised by ``--expire`` parsing.
_DURATION_UNITS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


class AuthMode(StrEnum):
    """Auth modes supported by ``preview start --auth``."""

    BASIC = "basic"
    TOKEN = "token"
    NONE = "none"


class PreviewError(RuntimeError):
    """Raised by :class:`PreviewManager` when start/stop fails."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_duration(spec: str | int | float | None, *, default: int = DEFAULT_EXPIRE_SECONDS) -> int:
    """Parse a duration string like ``"30m"`` / ``"4h"`` into seconds.

    Plain numbers are interpreted as seconds. ``None`` falls back to
    *default*.

    Args:
        spec: ``"30m"``, ``"4h"``, ``"3600"``, ``3600``, ``None`` …
        default: Seconds returned when *spec* is empty.

    Returns:
        Duration in seconds, always strictly positive.

    Raises:
        ValueError: If *spec* cannot be parsed.
    """
    if spec is None or spec == "":
        return default
    if isinstance(spec, (int, float)):
        seconds = int(spec)
        if seconds <= 0:
            raise ValueError(f"duration must be > 0 seconds: {spec!r}")
        return seconds
    text = str(spec).strip().lower()
    if not text:
        return default
    match = re.fullmatch(r"(\d+)([smhd]?)", text)
    if match is None:
        raise ValueError(f"invalid duration spec: {spec!r}")
    number = int(match.group(1))
    unit = match.group(2) or "s"
    seconds = number * _DURATION_UNITS[unit]
    if seconds <= 0:
        raise ValueError(f"duration must be > 0 seconds: {spec!r}")
    return seconds


@dataclass
class PreviewState:
    """Persisted record of a live preview.

    Attributes:
        preview_id: Stable opaque id printed by ``preview list``.
        command: Resolved command string the manager dispatched.
        cwd: Working directory the command ran in (== sandbox workdir).
        port: Local TCP port the dev server bound to.
        sandbox_backend: Backend name (``"worktree"``, ``"docker"``, …).
        sandbox_session_id: Backend-supplied session identifier.
        tunnel_provider: Provider name (``"cloudflared"``, ``"ngrok"`` …).
        tunnel_name: Tunnel name registered with the registry - used to
            stop the tunnel on shutdown.
        public_url: Bare tunnel URL (no auth credentials).
        share_url: URL with auth credentials baked in (token query or
            basic ``user:pass@``). Equal to ``public_url`` for ``none``.
        auth_mode: ``"basic"``, ``"token"`` or ``"none"``.
        expires_at_epoch: Unix timestamp at which the share URL expires.
        process_pid: PID of the dev-server process tree leader.
        created_at_epoch: When the preview was created.
        container_name: Isolated container name when backed by a container runtime.
        process_start_time: ``psutil.Process.create_time()`` captured at spawn
            to bind :attr:`process_pid` to the process instance across sessions,
            or ``None`` when ``psutil`` is unavailable or for container-backed
            or legacy state records.
        process_start_ticks: Linux ``/proc/<pid>/stat`` field 22 monotonic
            ``starttime`` ticks captured at spawn (immune to ``btime`` NTP
            shifts), or ``None`` on non-Linux hosts or legacy state records.
    """

    preview_id: str
    command: str
    cwd: str
    port: int
    sandbox_backend: str
    sandbox_session_id: str
    tunnel_provider: str
    tunnel_name: str
    public_url: str
    share_url: str
    auth_mode: str
    expires_at_epoch: float
    process_pid: int
    created_at_epoch: float = field(default_factory=time.time)
    container_name: str | None = None
    process_start_time: float | None = None
    process_start_ticks: int | None = None

    def is_expired(self, *, now: float | None = None) -> bool:
        """Return ``True`` when *now* is past :attr:`expires_at_epoch`."""
        if self.expires_at_epoch <= 0:
            return False
        ts = now if now is not None else time.time()
        return ts >= self.expires_at_epoch

    def to_dict(self) -> dict[str, Any]:
        """Render the dataclass as a plain dict for JSON encoding."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PreviewState:
        """Reconstruct a state record from JSON.

        Unknown keys are dropped so older orchestrator versions can
        still read newer state files.
        """
        keys = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in raw.items() if k in keys}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class PreviewStore:
    """JSON-backed registry of :class:`PreviewState` records.

    Args:
        path: Override of the state-file location. Defaults to
            ``.sdd/runtime/preview/state.json``.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or PREVIEW_STATE_FILE
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        """Return the state-file path."""
        return self._path

    def list(self) -> builtins.list[PreviewState]:
        """Return every persisted preview record."""
        with self._lock:
            return self._load()

    def get(self, preview_id: str) -> PreviewState | None:
        """Return the persisted state for *preview_id* or ``None``."""
        for state in self.list():
            if state.preview_id == preview_id:
                return state
        return None

    def upsert(self, state: PreviewState) -> None:
        """Insert or replace *state* in the persisted list."""
        with self._lock:
            records = self._load()
            records = [s for s in records if s.preview_id != state.preview_id]
            records.append(state)
            self._save(records)

    def remove(self, preview_id: str) -> bool:
        """Remove the record matching *preview_id*; return ``True`` if found."""
        with self._lock:
            records = self._load()
            kept = [s for s in records if s.preview_id != preview_id]
            if len(kept) == len(records):
                return False
            self._save(kept)
            return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> builtins.list[PreviewState]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Preview state read failed for %s: %s", self._path, exc)
            return []
        items = raw.get("previews", []) if isinstance(raw, dict) else []
        out: builtins.list[PreviewState] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                out.append(PreviewState.from_dict(item))
            except (TypeError, KeyError) as exc:
                logger.debug("Skipping malformed preview state row: %s", exc)
        return out

    def _save(self, records: builtins.list[PreviewState]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"previews": [r.to_dict() for r in records]}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)


# ---------------------------------------------------------------------------
# Public Preview record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preview:
    """Result returned by :meth:`PreviewManager.start`.

    Attributes:
        state: Persisted record describing the live preview.
        auth: Issued credentials. ``None`` for ``auth_mode == "none"``.
    """

    state: PreviewState
    auth: IssuedAuth | None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class PreviewManager:
    """Drive ``preview start|stop|list|status``.

    Args:
        store: Persistence backend. A default one is created when
            omitted.
        tunnel: Bridge to the existing ``bernstein tunnel`` wrapper.
            Tests may inject a fake.
        token_issuer: Signed-token issuer. Tests may inject a fake.
        audit_log: HMAC-chained audit log used for every state change.
            When ``None``, a default :class:`AuditLog` rooted at
            ``.sdd/audit`` is constructed.
        runner: Override of the dev-server launcher used by tests. The
            default uses :mod:`subprocess` and expects a
            :class:`SandboxSession` to provide the working directory.
        clock: Optional clock override.
    """

    def __init__(
        self,
        *,
        store: PreviewStore | None = None,
        tunnel: TunnelBridge | None = None,
        token_issuer: PreviewTokenIssuer | None = None,
        audit_log: AuditLog | None = None,
        runner: DevServerRunner | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = store or PreviewStore()
        self._tunnel = tunnel or TunnelBridge()
        self._issuer = token_issuer or PreviewTokenIssuer(
            secret=_default_token_secret(),
        )
        self._audit = audit_log
        self._runner = runner or ContainerDevServerRunner()
        self._clock: Clock = clock or _SystemClock()
        self._last_skipped_pids: builtins.list[tuple[str, int, str]] = []

    @property
    def last_skipped_pids(self) -> tuple[tuple[str, int, str], ...]:
        """Return ``(preview_id, pid, reason)`` tuples skipped by the last stop/reap call."""
        return tuple(self._last_skipped_pids)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list(self) -> builtins.list[PreviewState]:
        """Return every active preview the manager knows about."""
        return self._store.list()

    def status(self, preview_id: str) -> PreviewState | None:
        """Return state for *preview_id* or ``None``."""
        return self._store.get(preview_id)

    def discover(self, cwd: Path) -> builtins.list[DiscoveredCommand]:
        """Return every discovered candidate command under *cwd*."""
        return list_candidates(cwd)

    def start(
        self,
        *,
        cwd: Path,
        sandbox_session: SandboxLike,
        command: str | None = None,
        approve_discovered: bool = False,
        provider: str = "auto",
        auth_mode: AuthMode | str = AuthMode.TOKEN,
        expire_seconds: int | str | None = None,
        port_probe_timeout: float = 30.0,
        clock: Clock | None = None,
    ) -> Preview:
        """Boot the dev server and return the resulting :class:`Preview`.

        Args:
            cwd: Working directory. Most callers pass the resolved
                worktree path of the originating session.
            sandbox_session: A :class:`SandboxBackend`-style session
                whose ``backend_name`` and ``session_id`` are recorded
                on the :class:`PreviewState`. Passed in so the manager
                stays decoupled from the concrete backend selection.
            command: Optional explicit command override. ``None`` means
                "auto-discover".
            provider: Tunnel provider - defaults to ``"auto"`` per the
                ticket.
            auth_mode: ``"basic"``, ``"token"`` or ``"none"``.
            expire_seconds: ``--expire`` value. Accepts strings like
                ``"30m"`` or raw integers; defaults to 4h.
            port_probe_timeout: TCP-probe budget in seconds.
            clock: Optional clock override for tests.

        Returns:
            A :class:`Preview` describing the live preview.

        Raises:
            PreviewError: When discovery, port detection, the TCP
                probe, the tunnel, or auth issuance fails. On any of
                those the manager rolls back so the system never leaks
                a half-started preview.
        """
        active_clock = clock or self._clock
        chosen = _resolve_command(cwd, command)
        if chosen.source != "--command" and not approve_discovered:
            raise PreviewError("Workspace-discovered commands require explicit approval")
        expire_total = parse_duration(expire_seconds, default=DEFAULT_EXPIRE_SECONDS)
        normalised_mode = _normalise_auth(auth_mode)
        preview_id = f"prv-{secrets.token_hex(4)}"

        run_handle = self._runner.spawn(command=chosen.command, cwd=cwd)
        process_start_time = _read_process_create_time(run_handle.pid)
        process_start_ticks = _read_linux_process_start_ticks(run_handle.pid)
        try:
            port = _await_port(run_handle, timeout_seconds=port_probe_timeout)
        except Exception as exc:
            self._runner.terminate(run_handle)
            raise PreviewError(f"Could not detect dev-server port: {exc}") from exc

        if not probe_port(port, timeout_seconds=port_probe_timeout, clock=active_clock.monotonic):
            self._runner.terminate(run_handle)
            raise PreviewError(
                f"Dev server bound port {port} but TCP probe failed within {port_probe_timeout:.0f}s; aborting."
            )

        try:
            handle = self._tunnel.open(port=port, provider=provider, name=preview_id)
        except TunnelBridgeError as exc:
            # Roll back the dev server when the tunnel can't open.
            self._runner.terminate(run_handle)
            raise PreviewError(f"Tunnel start failed: {exc}") from exc

        try:
            issued = self._issuer.issue(
                preview_id=preview_id,
                mode=normalised_mode.value,
                expires_in_seconds=expire_total,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                self._tunnel.close(handle.name)
            self._runner.terminate(run_handle)
            raise PreviewError(f"Auth issuance failed: {exc}") from exc

        state = PreviewState(
            preview_id=preview_id,
            command=chosen.command or "",
            cwd=str(cwd.resolve()),
            port=port,
            sandbox_backend=run_handle.backend or getattr(sandbox_session, "backend_name", "unknown"),
            sandbox_session_id=run_handle.container_name or getattr(sandbox_session, "session_id", "unknown"),
            tunnel_provider=handle.provider,
            tunnel_name=handle.name,
            public_url=handle.public_url,
            share_url=issued.render_url(handle.public_url) if issued.mode != "none" else handle.public_url,
            auth_mode=issued.mode,
            expires_at_epoch=(
                issued.expires_at_epoch if issued.expires_at_epoch > 0 else active_clock.now() + expire_total
            ),
            process_pid=run_handle.pid,
            container_name=run_handle.container_name,
            process_start_time=process_start_time,
            process_start_ticks=process_start_ticks,
            created_at_epoch=active_clock.now(),
        )
        try:
            self._store.upsert(state)
        except OSError as exc:
            with contextlib.suppress(Exception):
                self._tunnel.close(handle.name)
            self._runner.terminate(run_handle)
            raise PreviewError("Could not persist preview state") from exc

        # Audit + metrics - issued before returning so observers see the
        # preview the moment ``start`` returns.
        self._record_audit(
            "preview.start",
            actor="bernstein.preview",
            resource_id=preview_id,
            details={
                "command": state.command,
                "cwd": state.cwd,
                "port": state.port,
                "tunnel_provider": state.tunnel_provider,
                "tunnel_name": state.tunnel_name,
                "sandbox_backend": state.sandbox_backend,
                "sandbox_session_id": state.sandbox_session_id,
                "auth_mode": state.auth_mode,
            },
        )
        self._record_audit(
            "preview.link",
            actor="bernstein.preview",
            resource_id=preview_id,
            details={
                "auth_mode": state.auth_mode,
                "tunnel_provider": state.tunnel_provider,
                "expires_at_epoch": state.expires_at_epoch,
            },
        )
        record_preview_started(provider=state.tunnel_provider, sandbox=state.sandbox_backend)
        record_link_issued(auth_mode=state.auth_mode)
        return Preview(state=state, auth=issued if issued.mode != "none" else None)

    def stop(self, preview_id: str) -> bool:
        """Stop a single preview by id.

        Args:
            preview_id: Identifier returned by :meth:`start`.

        Returns:
            ``True`` if a preview was stopped, ``False`` if no record
            matched.
        """
        self._last_skipped_pids = []
        state = self._store.get(preview_id)
        if state is None:
            return False
        self._teardown(state, reason="manual")
        return True

    def stop_all(self) -> int:
        """Stop every active preview. Returns the number stopped."""
        self._last_skipped_pids = []
        states = self._store.list()
        for state in states:
            self._teardown(state, reason="all")
        return len(states)

    def reap_expired(self, *, now: float | None = None) -> int:
        """Tear down every preview whose ``expires_at_epoch`` has passed.

        Args:
            now: Optional override of the current time; defaults to
                :func:`time.time`.

        Returns:
            Count of previews torn down.
        """
        self._last_skipped_pids = []
        ts = now if now is not None else self._clock.now()
        reaped = 0
        for state in self._store.list():
            if state.is_expired(now=ts):
                try:
                    self._teardown(state, reason="expired")
                except PreviewError as exc:
                    logger.warning("Could not reap preview %s: %s", state.preview_id, exc)
                else:
                    reaped += 1
        return reaped

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _teardown(self, state: PreviewState, *, reason: str) -> None:
        """Close the tunnel, kill the dev server, drop the record, audit."""
        with contextlib.suppress(Exception):
            self._tunnel.close(state.tunnel_name)
        pid_signal_status: str | None = None
        pid_signal_reason: str | None = None
        if state.container_name is not None:
            try:
                runtime = runtime_executable(state.sandbox_backend)
            except IsolationUnavailable as exc:
                raise PreviewError("Restore the preview runtime before retrying stop") from exc
            if not _remove_preview_container(runtime, state.container_name):
                raise PreviewError(
                    "Preview container cleanup could not be confirmed. Retry stop after restoring the runtime."
                )
        # Integer 0 is the no-host-PID sentinel from DevServerHandle(0, ...)
        # (e.g. container-backed or synthetic handles); route bools, negatives,
        # PID 1, and live host PIDs through the fail-closed guard.
        elif isinstance(state.process_pid, bool) or state.process_pid != 0:
            signaled, pid_reason = _terminate_pid(
                state.process_pid,
                expected_start_time=state.process_start_time,
                expected_start_ticks=state.process_start_ticks,
            )  # NOSONAR python:S4828
            pid_signal_status = "signaled" if signaled else "skipped"
            pid_signal_reason = pid_reason
            if not signaled:
                self._last_skipped_pids.append((state.preview_id, state.process_pid, pid_reason))
                logger.warning(
                    "Skipping termination of preview %s PID %r (%s); cleaning up state",
                    state.preview_id,
                    state.process_pid,
                    pid_reason,
                )
        self._store.remove(state.preview_id)
        record_preview_stopped(provider=state.tunnel_provider, sandbox=state.sandbox_backend)
        audit_details: dict[str, Any] = {
            "reason": reason,
            "tunnel_provider": state.tunnel_provider,
            "sandbox_backend": state.sandbox_backend,
        }
        if pid_signal_status is not None and pid_signal_reason is not None:
            audit_details["process_pid"] = state.process_pid
            audit_details["process_signal_status"] = pid_signal_status
            audit_details["process_signal_reason"] = pid_signal_reason
        self._record_audit(
            "preview.stop",
            actor="bernstein.preview",
            resource_id=state.preview_id,
            details=audit_details,
        )

    def _record_audit(
        self,
        event_type: str,
        *,
        actor: str,
        resource_id: str,
        details: dict[str, Any],
    ) -> None:
        """Emit an audit entry; demote audit failures to warnings.

        We intentionally never raise here - losing observability is
        bad, but losing the preview because the audit log is wedged
        is worse.
        """
        log = self._audit_log()
        if log is None:
            return
        try:
            log.log(
                event_type=event_type,
                actor=actor,
                resource_type="preview",
                resource_id=resource_id,
                details=details,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Audit log write failed for %s: %s", event_type, exc)

    def _audit_log(self) -> AuditLog | None:
        if self._audit is not None:
            return self._audit
        try:
            DEFAULT_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            self._audit = AuditLog(DEFAULT_AUDIT_DIR)
            return self._audit
        except Exception as exc:  # pragma: no cover - permissions etc.
            logger.warning("AuditLog unavailable: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Sandbox/clock/runner protocols
# ---------------------------------------------------------------------------


class SandboxLike:
    """Structural protocol describing the bits we read off a sandbox session.

    We deliberately accept *anything* with the two attributes - it lets
    callers pass either a real :class:`SandboxSession` or a lightweight
    test stub.
    """

    backend_name: str = "unknown"
    session_id: str = "unknown"


class Clock:
    """Tiny clock protocol so tests can move time around."""

    def now(self) -> float:  # pragma: no cover - trivial
        """Return current unix epoch seconds."""
        return time.time()

    def monotonic(self) -> float:  # pragma: no cover - trivial
        """Return monotonic seconds."""
        return time.monotonic()


class _SystemClock(Clock):
    """Default :class:`Clock` reading :func:`time`/`time.monotonic`."""


@dataclass
class DevServerHandle:
    """Opaque handle returned by a :class:`DevServerRunner`.

    Attributes:
        pid: PID of the spawned process tree leader.
        process: Underlying :class:`subprocess.Popen` (or stub).
        stdout_lines: Iterable of stdout lines for port capture. Tests
            inject deterministic iterables; the real runner attaches a
            background reader.
    """

    pid: int
    process: object
    stdout_lines: Any
    backend: str | None = None
    container_name: str | None = None
    host_port: int | None = None
    runtime_path: str | None = None


class DevServerRunner:
    """Strategy for launching the dev-server process.

    Tests inject fakes; production uses :class:`ContainerDevServerRunner`.
    """

    def spawn(self, *, command: str | None, cwd: Path) -> DevServerHandle:
        """Launch *command* in *cwd*; return a handle."""
        raise NotImplementedError

    def terminate(self, handle: DevServerHandle) -> None:
        """Best-effort termination of *handle*."""
        raise NotImplementedError


def _remove_preview_container(runtime: str, name: str) -> bool:
    if Path(runtime).stem.lower() not in ("docker", "podman") or not re.fullmatch(
        r"bernstein-preview-[a-f0-9]{16}", name
    ):
        return False
    try:
        # Podman can ignore an absent container. Docker reports absence.
        command = [runtime, "rm", "--force"]
        if Path(runtime).stem.lower() == "podman":
            command.append("--ignore")
        result = subprocess.run([*command, name], capture_output=True, timeout=10, check=False)
        return result.returncode == 0 or f"No such container: {name}".encode() in result.stderr
    except (OSError, subprocess.TimeoutExpired):
        return False


class ContainerDevServerRunner(DevServerRunner):
    """Start the approved argv with only a read-only workspace and loopback port."""

    def spawn(self, *, command: str | None, cwd: Path) -> DevServerHandle:
        try:
            argv = shlex.split(command or "")
            runtime = runtime_executable()
            mount = readonly_bind(cwd, "/workspace")
            container_port = int(os.environ.get("BERNSTEIN_PREVIEW_PORT", "3000"))
        except (ValueError, IsolationUnavailable) as exc:
            raise PreviewError(str(exc)) from exc
        image = os.environ.get("BERNSTEIN_PREVIEW_IMAGE", "")
        if not argv or argv[0].startswith("-") or not image or image.startswith("-"):
            raise PreviewError("An explicit argv and operator-configured BERNSTEIN_PREVIEW_IMAGE are required")
        if not 1 <= container_port <= 65535:
            raise PreviewError("Invalid BERNSTEIN_PREVIEW_PORT")
        backend = Path(runtime).stem.lower()
        name = f"bernstein-preview-{secrets.token_hex(8)}"
        # Only the loopback-published application port leaves the container.
        # The selected image is operator-owned and already installed locally.
        cmd = [
            runtime,
            "run",
            "--detach",
            "--name",
            name,
            "--pull=never",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--memory=1g",
            "--cpus=2",
            "--pids-limit=128",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=128m",
            "--log-driver=none",
            "--mount",
            mount,
            "--workdir=/workspace",
            "--env=HOME=/tmp",
            "--env=HOST=0.0.0.0",
            f"--env=PORT={container_port}",
            "--publish",
            f"127.0.0.1::{container_port}",
            "--entrypoint",
            argv[0],
            "--",
            image,
            *argv[1:],
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
            if result.returncode != 0:
                raise PreviewError("The isolated preview runtime could not start the configured image")
            mapping = subprocess.run(
                [runtime, "port", name, f"{container_port}/tcp"],
                capture_output=True,
                timeout=10,
                check=True,
            )
            match = re.fullmatch(rb"127\.0\.0\.1:([0-9]+)", mapping.stdout.strip())
            if match is None:
                raise PreviewError("The preview runtime did not assign a loopback port")
            return DevServerHandle(0, None, (), backend, name, int(match[1]), runtime)
        except (OSError, subprocess.SubprocessError, PreviewError) as exc:
            removed = _remove_preview_container(runtime, name)
            if not removed:
                logger.warning("Could not remove failed preview container %s", name)
            detail = str(exc) if isinstance(exc, PreviewError) else type(exc).__name__
            raise PreviewError(f"Could not start isolated dev server: {detail}") from exc

    def terminate(self, handle: DevServerHandle) -> None:
        if (
            handle.runtime_path
            and handle.container_name
            and not _remove_preview_container(handle.runtime_path, handle.container_name)
        ):
            logger.warning("Could not remove failed preview container %s", handle.container_name)


def _await_port(handle: DevServerHandle, *, timeout_seconds: float) -> int:
    """Walk *handle*'s stdout until a port is captured or *timeout* hits.

    Args:
        handle: Result of :meth:`DevServerRunner.spawn`.
        timeout_seconds: Wall-clock budget. Passed transparently to
            consumers - the caller is expected to enforce a hard upper
            bound on the iterable.

    Returns:
        The captured port.

    Raises:
        PortNotDetectedError: When the stdout iterable is exhausted (or
            the budget elapsed) without yielding a port.
    """
    if handle.host_port is not None:
        return handle.host_port
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    buffered: list[str] = []
    for line in handle.stdout_lines:
        buffered.append(line)
        port = capture_port([line])
        if port is not None:
            return port
        if time.monotonic() >= deadline:
            break
    # One last sweep against everything we buffered.
    final = capture_port(buffered)
    if final is not None:
        return final
    raise PortNotDetectedError("dev-server stdout did not advertise a port")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_command(cwd: Path, command: str | None) -> DiscoveredCommand:
    """Resolve a command - explicit override beats auto-discovery."""
    if command and command.strip():
        return DiscoveredCommand(source="--command", command=command.strip())
    discovered = discover_commands(cwd)
    if discovered is None:
        raise PreviewError(
            "No dev-server command discovered (looked at package.json, Procfile, "
            "bernstein.yaml). Pass --command to override."
        )
    return discovered


def _normalise_auth(mode: AuthMode | str) -> AuthMode:
    if isinstance(mode, AuthMode):
        return mode
    try:
        return AuthMode(str(mode).strip().lower())
    except ValueError as exc:
        raise PreviewError(f"unknown auth mode: {mode!r}") from exc


def _import_psutil() -> Any | None:
    """Lazily import ``psutil``; return ``None`` on minimal installs without it."""
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def _is_valid_target_pid(pid: object) -> TypeGuard[int]:
    """Return ``True`` when *pid* is an integer ``> 1`` and not a ``bool``."""
    return not isinstance(pid, bool) and isinstance(pid, int) and pid > 1


def _is_valid_start_time(value: Any) -> TypeGuard[int | float]:
    """Return ``True`` when *value* is a finite positive timestamp (not ``bool``)."""
    return (
        not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and float(value) > 0.0
    )


def _is_valid_start_ticks(value: Any) -> TypeGuard[int]:
    """Return ``True`` when *value* is a positive integer tick count (not ``bool``)."""
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _parse_linux_proc_stat_start_ticks(raw: bytes, *, expected_pid: int) -> int | None:
    """Extract field 22 (``starttime`` ticks) from a Linux ``/proc/<pid>/stat`` payload."""
    lpar = raw.find(b"(")
    rpar = raw.rfind(b")")
    if lpar <= 0 or rpar <= lpar:
        return None
    pid_bytes = raw[:lpar].strip()
    if not pid_bytes.isdigit() or int(pid_bytes) != expected_pid:
        return None
    # Everything after the final ')' starts at field 3 (state); field 22 (starttime) is index 19.
    fields = raw[rpar + 1 :].split()
    if len(fields) <= 19:
        return None
    raw_ticks = fields[19]
    if not raw_ticks.isdigit():
        return None
    ticks = int(raw_ticks)
    return ticks if ticks > 0 else None


def _read_linux_process_start_ticks(pid: object) -> int | None:
    """Return ``/proc/<pid>/stat`` field 22 ticks on Linux (or when ``_PROC_ROOT`` is overridden)."""
    if not _is_valid_target_pid(pid):
        return None
    if _PROC_ROOT.as_posix() == "/proc" and not sys.platform.startswith("linux"):
        return None
    try:
        raw = (_PROC_ROOT / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    return _parse_linux_proc_stat_start_ticks(raw, expected_pid=pid)


def _read_linux_process_start_ticks_checked(pid: int) -> tuple[int | None, str | None]:
    """Read live Linux ``starttime`` ticks and classify missing vs unreadable errors."""
    if _PROC_ROOT.as_posix() == "/proc" and not sys.platform.startswith("linux"):
        return None, "unreadable_start_ticks"
    try:
        raw = (_PROC_ROOT / str(pid) / "stat").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None, "not_alive"
    except OSError:
        return None, "unreadable_start_ticks"
    ticks = _parse_linux_proc_stat_start_ticks(raw, expected_pid=pid)
    if ticks is None:
        return None, "unreadable_start_ticks"
    return ticks, None


def _read_process_create_time(pid: object) -> float | None:
    """Return ``psutil.Process(pid).create_time()`` or ``None`` if unavailable/unreadable."""
    if not _is_valid_target_pid(pid):
        return None
    psutil_mod = _import_psutil()
    if psutil_mod is None:
        return None
    try:
        created = psutil_mod.Process(pid).create_time()
    except (psutil_mod.Error, OSError):
        return None
    if not _is_valid_start_time(created):
        return None
    return float(created)


def _terminate_pid(
    pid: object,
    expected_start_time: object = None,
    expected_start_ticks: object = None,
) -> tuple[bool, str]:
    """Fail-closed SIGTERM for a persisted preview dev-server PID.

    Refuses invalid/bool/init PIDs (``<= 1``), our own PID, any PID sharing
    our process group (or with ``pgid <= 1``), and any PID whose live start
    identity cannot be verified against *expected_start_ticks* (Linux
    ``/proc/<pid>/stat`` field 22) or *expected_start_time*
    (``psutil.Process(pid).create_time()``). When ``psutil`` is not installed
    and no kernel start-tick token is recorded, fails closed with
    ``"psutil_unavailable"``.

    Returns:
        ``(signaled, reason)`` where *signaled* is ``True`` only when
        ``SIGTERM`` was delivered.
    """
    if not _is_valid_target_pid(pid):
        return False, "invalid_pid"
    if pid == os.getpid():
        return False, "self_pid"

    own_pgid: int | None = None
    getpgrp = getattr(os, "getpgrp", None)
    if getpgrp is not None:
        try:
            own_pgid = int(getpgrp())
        except OSError:
            return False, "unreadable_pgid"
        if pid == own_pgid:
            return False, "own_process_group"

    if expected_start_ticks is not None:
        if not _is_valid_start_ticks(expected_start_ticks):
            return False, "invalid_start_ticks"
        expected_ticks: int | None = expected_start_ticks
    else:
        expected_ticks = None

    if expected_start_time is not None:
        if not _is_valid_start_time(expected_start_time):
            return False, "invalid_start_time"
        expected_time: float | None = float(expected_start_time)
    else:
        expected_time = None

    if expected_ticks is None and expected_time is None:
        return False, "missing_start_time"

    target_pgid: int | None = None
    getpgid = getattr(os, "getpgid", None)
    if getpgid is not None:
        try:
            target_pgid = int(getpgid(pid))
        except ProcessLookupError:
            return False, "not_alive"
        except OSError:
            return False, "unreadable_pgid"
        if own_pgid is not None and target_pgid == own_pgid:
            return False, "own_process_group"
        if target_pgid <= 1:
            return False, "unsafe_process_group"

    if expected_ticks is not None:
        actual_ticks, ticks_err = _read_linux_process_start_ticks_checked(pid)
        if ticks_err is not None or actual_ticks is None:
            return False, ticks_err or "unreadable_start_ticks"
        if actual_ticks != expected_ticks:
            return False, "start_time_mismatch"
    else:
        if expected_time is None:
            return False, "missing_start_time"
        psutil_mod = _import_psutil()
        if psutil_mod is None:
            return False, "psutil_unavailable"
        try:
            actual_start_time = psutil_mod.Process(pid).create_time()
        except psutil_mod.NoSuchProcess:
            return False, "not_alive"
        except (psutil_mod.Error, OSError):
            return False, "unreadable_start_time"

        if not _is_valid_start_time(actual_start_time):
            return False, "unreadable_start_time"
        if abs(float(actual_start_time) - expected_time) > _PROCESS_START_TIME_TOLERANCE_SECONDS:
            return False, "start_time_mismatch"

    # Signal the process group only when the verified PID is its group leader
    # (already verified > 1 and distinct from our own process group).
    killpg = getattr(os, "killpg", None)
    if target_pgid is not None and target_pgid == pid and killpg is not None:
        try:
            killpg(target_pgid, signal.SIGTERM)
            return True, "signaled"
        except ProcessLookupError:
            return False, "not_alive"
        except OSError:
            return False, "signal_failed"

    try:
        os.kill(pid, signal.SIGTERM)
        return True, "signaled"
    except ProcessLookupError:
        return False, "not_alive"
    except OSError:
        return False, "signal_failed"


def _default_token_secret() -> str:
    """Return a per-process token secret.

    Production callers should configure the secret explicitly; this is
    a sane fallback that keeps tokens valid for the orchestrator's
    lifetime even when no env var is set.
    """
    return os.environ.get("BERNSTEIN_PREVIEW_SECRET") or secrets.token_urlsafe(32)


__all__ = [
    "DEFAULT_AUDIT_DIR",
    "DEFAULT_EXPIRE_SECONDS",
    "PREVIEW_STATE_DIR",
    "PREVIEW_STATE_FILE",
    "AuthMode",
    "Clock",
    "ContainerDevServerRunner",
    "DevServerHandle",
    "DevServerRunner",
    "Preview",
    "PreviewError",
    "PreviewManager",
    "PreviewState",
    "PreviewStore",
    "SandboxLike",
    "parse_duration",
]
