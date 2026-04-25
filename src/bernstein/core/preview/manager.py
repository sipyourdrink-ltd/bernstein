"""``PreviewManager`` — orchestrates a single ``bernstein preview`` lifecycle.

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
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

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
from bernstein.core.security.audit import AuditLog

logger = logging.getLogger(__name__)


PREVIEW_STATE_DIR = Path(".sdd/runtime/preview")
PREVIEW_STATE_FILE = PREVIEW_STATE_DIR / "state.json"
DEFAULT_AUDIT_DIR = Path(".sdd/audit")

DEFAULT_EXPIRE_SECONDS = 4 * 3600  # 4 hours, per the ticket.

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
        tunnel_name: Tunnel name registered with the registry — used to
            stop the tunnel on shutdown.
        public_url: Bare tunnel URL (no auth credentials).
        share_url: URL with auth credentials baked in (token query or
            basic ``user:pass@``). Equal to ``public_url`` for ``none``.
        auth_mode: ``"basic"``, ``"token"`` or ``"none"``.
        expires_at_epoch: Unix timestamp at which the share URL expires.
        process_pid: PID of the dev-server process tree leader.
        created_at_epoch: When the preview was created.
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

    def list(self) -> list[PreviewState]:
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

    def _load(self) -> list[PreviewState]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Preview state read failed for %s: %s", self._path, exc)
            return []
        items = raw.get("previews", []) if isinstance(raw, dict) else []
        out: list[PreviewState] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                out.append(PreviewState.from_dict(item))
            except (TypeError, KeyError) as exc:
                logger.debug("Skipping malformed preview state row: %s", exc)
        return out

    def _save(self, records: list[PreviewState]) -> None:
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
        self._runner = runner or SubprocessDevServerRunner()
        self._clock: Clock = clock or _SystemClock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list(self) -> list[PreviewState]:
        """Return every active preview the manager knows about."""
        return self._store.list()

    def status(self, preview_id: str) -> PreviewState | None:
        """Return state for *preview_id* or ``None``."""
        return self._store.get(preview_id)

    def discover(self, cwd: Path) -> list[DiscoveredCommand]:
        """Return every discovered candidate command under *cwd*."""
        return list_candidates(cwd)

    def start(
        self,
        *,
        cwd: Path,
        sandbox_session: SandboxLike,
        command: str | None = None,
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
            provider: Tunnel provider — defaults to ``"auto"`` per the
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
        expire_total = parse_duration(expire_seconds, default=DEFAULT_EXPIRE_SECONDS)
        normalised_mode = _normalise_auth(auth_mode)
        preview_id = f"prv-{secrets.token_hex(4)}"

        run_handle = self._runner.spawn(command=chosen.command, cwd=cwd)
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
            sandbox_backend=getattr(sandbox_session, "backend_name", "unknown"),
            sandbox_session_id=getattr(sandbox_session, "session_id", "unknown"),
            tunnel_provider=handle.provider,
            tunnel_name=handle.name,
            public_url=handle.public_url,
            share_url=issued.render_url(handle.public_url) if issued.mode != "none" else handle.public_url,
            auth_mode=issued.mode,
            expires_at_epoch=(
                issued.expires_at_epoch if issued.expires_at_epoch > 0 else active_clock.now() + expire_total
            ),
            process_pid=run_handle.pid,
            created_at_epoch=active_clock.now(),
        )
        self._store.upsert(state)

        # Audit + metrics — issued before returning so observers see the
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
        state = self._store.get(preview_id)
        if state is None:
            return False
        self._teardown(state, reason="manual")
        return True

    def stop_all(self) -> int:
        """Stop every active preview. Returns the number stopped."""
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
        ts = now if now is not None else self._clock.now()
        reaped = 0
        for state in self._store.list():
            if state.is_expired(now=ts):
                self._teardown(state, reason="expired")
                reaped += 1
        return reaped

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _teardown(self, state: PreviewState, *, reason: str) -> None:
        """Close the tunnel, kill the dev server, drop the record, audit."""
        with contextlib.suppress(Exception):
            self._tunnel.close(state.tunnel_name)
        if state.process_pid > 0:
            with contextlib.suppress(ProcessLookupError, OSError):
                # SIGTERM the dev server's process group so child
                # processes die too. Sandbox-managed PIDs are always
                # ones we spawned (Sonar python:S4828).
                _terminate_pid(state.process_pid)  # NOSONAR python:S4828
        self._store.remove(state.preview_id)
        record_preview_stopped(provider=state.tunnel_provider, sandbox=state.sandbox_backend)
        self._record_audit(
            "preview.stop",
            actor="bernstein.preview",
            resource_id=state.preview_id,
            details={
                "reason": reason,
                "tunnel_provider": state.tunnel_provider,
                "sandbox_backend": state.sandbox_backend,
            },
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

        We intentionally never raise here — losing observability is
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

    We deliberately accept *anything* with the two attributes — it lets
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


class DevServerRunner:
    """Strategy for launching the dev-server process.

    Tests inject fakes; production uses :class:`SubprocessDevServerRunner`.
    """

    def spawn(self, *, command: str | None, cwd: Path) -> DevServerHandle:
        """Launch *command* in *cwd*; return a handle."""
        raise NotImplementedError

    def terminate(self, handle: DevServerHandle) -> None:
        """Best-effort termination of *handle*."""
        raise NotImplementedError


class SubprocessDevServerRunner(DevServerRunner):
    """Real :class:`DevServerRunner` using :class:`subprocess.Popen`.

    The runner streams stdout into a queue so the manager can read
    lines incrementally without blocking on the child process.
    """

    def spawn(self, *, command: str | None, cwd: Path) -> DevServerHandle:
        if not command:
            raise PreviewError("Cannot spawn an empty command")
        # The dev server may be a free-form shell pipeline (``npm run
        # dev`` is fine, ``cd foo && npm run dev`` is not — we forbid
        # ``;`` and ``&&`` chaining to keep the audit trail honest).
        if any(token in command for token in (";", "&&", "||")):
            raise PreviewError("Pipeline / chained commands are not allowed")
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        # The reader goroutine accumulates stdout; the main thread can
        # poll it via ``stdout_lines`` (a generator).
        return DevServerHandle(
            pid=proc.pid,
            process=proc,
            stdout_lines=_iter_stdout(proc),
        )

    def terminate(self, handle: DevServerHandle) -> None:
        if handle.pid <= 0:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            _terminate_pid(handle.pid)
        # Wait briefly so we don't leak a zombie.
        proc = handle.process
        wait = getattr(proc, "wait", None)
        if callable(wait):
            with contextlib.suppress(Exception):
                wait(timeout=5)


def _iter_stdout(proc: subprocess.Popen[str]):  # type: ignore[no-untyped-def]
    """Yield decoded stdout lines from *proc* until it exits."""
    stream = proc.stdout
    if stream is None:
        return
    for raw in iter(stream.readline, ""):
        yield raw.rstrip("\n")
    stream.close()


def _await_port(handle: DevServerHandle, *, timeout_seconds: float) -> int:
    """Walk *handle*'s stdout until a port is captured or *timeout* hits.

    Args:
        handle: Result of :meth:`DevServerRunner.spawn`.
        timeout_seconds: Wall-clock budget. Passed transparently to
            consumers — the caller is expected to enforce a hard upper
            bound on the iterable.

    Returns:
        The captured port.

    Raises:
        PortNotDetectedError: When the stdout iterable is exhausted (or
            the budget elapsed) without yielding a port.
    """
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
    """Resolve a command — explicit override beats auto-discovery."""
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


def _terminate_pid(pid: int) -> None:
    """SIGTERM *pid*'s process group, falling back to the bare pid."""
    if pid <= 0:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return
    except (ProcessLookupError, PermissionError, OSError):
        # Fall through to a direct SIGTERM below.
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)


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
    "DevServerHandle",
    "DevServerRunner",
    "Preview",
    "PreviewError",
    "PreviewManager",
    "PreviewState",
    "PreviewStore",
    "SandboxLike",
    "SubprocessDevServerRunner",
    "parse_duration",
]
