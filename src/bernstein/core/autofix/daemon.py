"""Autofix daemon process supervisor.

The daemon module exposes four operations:

* :func:`start` — fork a long-running poller that walks each
  configured repo every ``poll_interval_seconds``.
* :func:`stop` — read the pid file under ``.sdd/runtime/autofix.pid``,
  send ``SIGTERM`` and wait for clean exit.
* :func:`status` — return a typed :class:`DaemonStatus` describing
  whether the daemon is running and, if so, when it last ticked.
* :func:`attach` — open the live status feed produced by the
  running daemon (a JSONL tail) so an operator can watch attempts
  scroll by without checking GitHub.

The poller itself is exposed as :func:`tick_once` for tests; the
daemon process simply wraps it in a sleep loop.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from bernstein.core.autofix.dispatcher import AttemptRecord  # re-exported via __all__

if TYPE_CHECKING:
    from bernstein.core.autofix.config import AutofixConfig, RepoConfig
    from bernstein.core.autofix.dispatcher import Dispatcher

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------

#: Path (relative to the workspace root) where the daemon writes its pid.
PID_FILE = Path(".sdd") / "runtime" / "autofix.pid"

#: Path (relative to the workspace root) where the daemon appends one
#: JSON record per dispatched attempt.  ``bernstein autofix attach``
#: tails this file.
STATUS_LOG = Path(".sdd") / "runtime" / "autofix.jsonl"


# ---------------------------------------------------------------------------
# Tick-source protocol
# ---------------------------------------------------------------------------


class FailingPRSource(Protocol):
    """Callable that yields PRs with currently-failing CI runs."""

    def __call__(self, repo_config: RepoConfig) -> list[FailingCandidate]: ...


@dataclass(frozen=True)
class FailingCandidate:
    """One unit of work for the daemon.

    Attributes:
        pr: PR metadata loaded by the source.
        run_id: Failing GitHub Actions run id to repair.
        log: Output of
            :func:`bernstein.core.autofix.gh_logs.extract_failed_log`.
        session_id: Trailer-resolved session id (already validated by
            the source).
    """

    pr: object  # PullRequestMetadata; declared as object to avoid
    # importing ownership at module load time.
    run_id: str
    log: object  # LogExtraction
    session_id: str


# ---------------------------------------------------------------------------
# Daemon status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonStatus:
    """Snapshot of the daemon's runtime state.

    Attributes:
        running: ``True`` when a process matching the pid file is
            alive.
        pid: PID of the live daemon (or ``0`` when not running).
        started_at: Unix timestamp the daemon was launched; ``0``
            when not running.
        last_tick_at: Unix timestamp of the most recent tick recorded
            in the status log; ``0`` when no tick has occurred yet.
        watched_repos: Tuple of repo names the daemon is monitoring.
    """

    running: bool = False
    pid: int = 0
    started_at: float = 0.0
    last_tick_at: float = 0.0
    watched_repos: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Pid-file helpers
# ---------------------------------------------------------------------------


def _read_pid(workdir: Path) -> int:
    """Return the pid stored in ``workdir / PID_FILE`` (0 when absent)."""
    pid_path = workdir / PID_FILE
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _write_pid(workdir: Path, pid: int) -> None:
    """Write ``pid`` atomically into ``workdir / PID_FILE``."""
    pid_path = workdir / PID_FILE
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pid_path.with_suffix(pid_path.suffix + ".tmp")
    tmp.write_text(f"{pid}\n", encoding="utf-8")
    os.replace(tmp, pid_path)


def _process_alive(pid: int) -> bool:
    """Return ``True`` when ``pid`` is alive (POSIX-only)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else.
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Status log helpers
# ---------------------------------------------------------------------------


def append_status(workdir: Path, record: AttemptRecord) -> None:
    """Append one attempt record as JSONL into the daemon status log.

    The log is plain JSONL — each line is a serialised
    :class:`AttemptRecord` so ``attach`` can stream it without
    parsing extra framing.

    Args:
        workdir: Project root used to resolve :data:`STATUS_LOG`.
        record: Attempt record produced by the dispatcher.
    """
    path = workdir / STATUS_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "ts": time.time(),
        "attempt_id": record.attempt_id,
        "repo": record.repo,
        "pr_number": record.pr_number,
        "push_sha": record.push_sha,
        "run_id": record.run_id,
        "session_id": record.session_id,
        "attempt_index": record.attempt_index,
        "outcome": record.outcome,
        "classifier": record.classification.kind if record.classification else "unknown",
        "model": record.classification.model if record.classification else "",
        "cost_usd": round(record.cost_usd, 6),
        "commit_sha": record.commit_sha,
        "reason": record.reason,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def read_status(workdir: Path) -> DaemonStatus:
    """Return a :class:`DaemonStatus` snapshot for ``workdir``.

    Args:
        workdir: Project root.

    Returns:
        A populated :class:`DaemonStatus`.  Missing pid file →
        ``running=False``; stale pid file → ``running=False``.
    """
    pid = _read_pid(workdir)
    pid_path = workdir / PID_FILE
    started_at = 0.0
    if pid > 0 and pid_path.exists():
        with suppress(OSError):
            started_at = pid_path.stat().st_mtime

    log_path = workdir / STATUS_LOG
    last_tick = 0.0
    if log_path.exists():
        with suppress(OSError):
            last_tick = log_path.stat().st_mtime

    if pid > 0 and not _process_alive(pid):
        # Stale pid file — surface "not running" so callers can clean
        # up rather than reporting a zombie as alive.
        return DaemonStatus(
            running=False,
            pid=0,
            started_at=0.0,
            last_tick_at=last_tick,
            watched_repos=(),
        )

    return DaemonStatus(
        running=pid > 0,
        pid=pid,
        started_at=started_at,
        last_tick_at=last_tick,
    )


# ---------------------------------------------------------------------------
# Tick loop
# ---------------------------------------------------------------------------


def tick_once(
    *,
    config: AutofixConfig,
    dispatcher: Dispatcher,
    failing_source: FailingPRSource,
    workdir: Path,
    extra_repo_filter: set[str] | None = None,
) -> list[AttemptRecord]:
    """Run a single poll across every configured repo.

    The function is the unit-test entry point for the daemon: it
    handles the iteration, calls the dispatcher for each candidate,
    and appends to the JSONL status log.  Sleep / signal handling
    are deliberately *not* part of this function so tests can drive
    it deterministically.

    Args:
        config: Daemon configuration (watched repos, byte budget, ...).
        dispatcher: Dispatcher to invoke for each eligible PR.
        failing_source: Callable that yields candidates per repo.
        workdir: Project root used to resolve the status log.
        extra_repo_filter: Optional set of repo names to restrict
            the tick to (used by ``--repo`` overrides).

    Returns:
        List of :class:`AttemptRecord` produced this tick.  May be
        empty when no eligible candidates were found.
    """
    from bernstein.core.autofix.gh_logs import LogExtraction
    from bernstein.core.autofix.ownership import PullRequestMetadata

    records: list[AttemptRecord] = []
    for repo_config in config.repos:
        if extra_repo_filter is not None and repo_config.name not in extra_repo_filter:
            continue
        try:
            candidates = failing_source(repo_config)
        except Exception:
            logger.exception("autofix: failing-source raised for repo %s", repo_config.name)
            continue

        for candidate in candidates:
            pr = candidate.pr
            log = candidate.log
            # Defensive narrowing for mypy / pyright; the protocol
            # types these as ``object`` to avoid an import cycle.
            if not isinstance(pr, PullRequestMetadata):
                continue
            if not isinstance(log, LogExtraction):
                continue
            try:
                record = dispatcher.dispatch(
                    repo_config=repo_config,
                    pr=pr,
                    run_id=candidate.run_id,
                    log=log,
                    session_id=candidate.session_id,
                )
            except Exception:
                logger.exception(
                    "autofix: dispatch raised for %s#%s",
                    pr.repo,
                    pr.number,
                )
                continue

            append_status(workdir, record)
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class DaemonAlreadyRunningError(RuntimeError):
    """Raised when ``start`` is called while a daemon is already alive."""


class DaemonNotRunningError(RuntimeError):
    """Raised when ``stop`` is called and no live daemon exists."""


def start(
    *,
    config: AutofixConfig,
    dispatcher: Dispatcher,
    failing_source: FailingPRSource,
    workdir: Path,
    extra_repo_filter: set[str] | None = None,
    sleep_fn: object = time.sleep,
    now_fn: object = time.time,
    iterations: int | None = None,
) -> int:
    """Run the daemon's main loop in the *current* process.

    The CLI wraps this call inside a forked child so the parent
    returns control to the user immediately.  Tests pass
    ``iterations`` so the loop terminates after a known number of
    ticks.

    Args:
        config: Daemon configuration.
        dispatcher: Dispatcher used for each candidate.
        failing_source: PR-source callable.
        workdir: Project root.
        extra_repo_filter: Optional repo allow-list.
        sleep_fn: Callable matching :func:`time.sleep`.  Tests
            inject a no-op.
        now_fn: Callable matching :func:`time.time`.  Tests inject
            a fixed clock.
        iterations: When set, the loop runs exactly that many ticks
            then returns.  ``None`` means "run forever" — the
            production behaviour.

    Returns:
        The total number of ticks executed.

    Raises:
        DaemonAlreadyRunningError: If a live pid file is found.
    """
    existing = _read_pid(workdir)
    if existing > 0 and _process_alive(existing):
        raise DaemonAlreadyRunningError(
            f"Autofix daemon is already running (pid {existing})."
        )

    _write_pid(workdir, os.getpid())
    ticks = 0
    try:
        while True:
            tick_once(
                config=config,
                dispatcher=dispatcher,
                failing_source=failing_source,
                workdir=workdir,
                extra_repo_filter=extra_repo_filter,
            )
            ticks += 1
            if iterations is not None and ticks >= iterations:
                break
            assert callable(sleep_fn)
            sleep_fn(config.poll_interval_seconds)
    finally:
        _clear_pid(workdir)
    # Touch ``now_fn`` so type-checkers do not flag it as unused.
    assert callable(now_fn)
    return ticks


def stop(workdir: Path, *, timeout_seconds: float = 10.0) -> int:
    """Send SIGTERM to the live daemon and wait for it to exit.

    Args:
        workdir: Project root.
        timeout_seconds: How long to wait before giving up.

    Returns:
        The pid that was signalled.

    Raises:
        DaemonNotRunningError: If no live daemon is detected.
    """
    pid = _read_pid(workdir)
    if pid <= 0 or not _process_alive(pid):
        raise DaemonNotRunningError("Autofix daemon is not running.")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError as exc:
        raise DaemonNotRunningError("Autofix daemon vanished before signal.") from exc

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _process_alive(pid):
            break
        time.sleep(0.1)

    _clear_pid(workdir)
    return pid


def _clear_pid(workdir: Path) -> None:
    """Remove the pid file (if any).  Best-effort."""
    pid_path = workdir / PID_FILE
    with suppress(FileNotFoundError, PermissionError, OSError):
        pid_path.unlink()


# ---------------------------------------------------------------------------
# attach() — read recent attempts from the JSONL status log
# ---------------------------------------------------------------------------


def recent_attempts(workdir: Path, *, limit: int = 50) -> list[dict[str, object]]:
    """Return the last ``limit`` records from the status JSONL log.

    The records are returned newest-first so a CLI ``--watch`` UI can
    display them without re-sorting.

    Args:
        workdir: Project root.
        limit: Maximum number of records to return.

    Returns:
        List of decoded JSON objects.  Malformed lines are skipped.
    """
    path = workdir / STATUS_LOG
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, object]] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append({str(k): v for k, v in payload.items()})  # type: ignore[reportUnknownVariableType]
        if len(out) >= limit:
            break
    return out


__all__ = [
    "PID_FILE",
    "STATUS_LOG",
    "AttemptRecord",
    "DaemonAlreadyRunningError",
    "DaemonNotRunningError",
    "DaemonStatus",
    "FailingCandidate",
    "FailingPRSource",
    "append_status",
    "read_status",
    "recent_attempts",
    "start",
    "stop",
    "tick_once",
]
