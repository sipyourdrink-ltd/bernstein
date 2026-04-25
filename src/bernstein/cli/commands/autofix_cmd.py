"""``bernstein autofix`` CLI group.

Operator-facing entry points for the autofix daemon — start, stop,
status, attach.  All real logic lives in
:mod:`bernstein.core.autofix.daemon`; this module is a thin click
wrapper that handles argument parsing and stdout formatting.

The CLI deliberately exposes :func:`start` *without* forking by
default — operators are expected to launch the daemon under
``bernstein daemon install`` (systemd / launchd) so the OS owns
restart logic.  The ``--foreground`` / ``--once`` flags exist
for tests and on-demand runs.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import click

from bernstein.core.autofix import (
    AutofixConfig,
    Dispatcher,
    load_config,
)
from bernstein.core.autofix.daemon import (
    DaemonAlreadyRunningError,
    DaemonNotRunningError,
    read_status,
    recent_attempts,
)
from bernstein.core.autofix.daemon import (
    start as daemon_start,
)
from bernstein.core.autofix.daemon import (
    stop as daemon_stop,
)
from bernstein.core.autofix.dispatcher import (
    AttemptCounter,
    DispatchResult,
)
from bernstein.core.security.audit import AuditLog

__all__ = ["autofix_group"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_workdir() -> Path:
    """Return the workspace root the daemon should operate over.

    The current working directory is the canonical answer; the
    helper exists so tests can monkey-patch a single function.
    """
    return Path.cwd()


def _placeholder_failing_source(
    *_: object,
    **__: object,
) -> list[object]:
    """Default ``FailingPRSource`` used by ``start`` when none is wired.

    The CLI does not yet ship a network-backed source — that lands in
    a follow-up — so the placeholder returns no candidates and lets
    the daemon idle politely.  Tests inject their own callable via
    :func:`bernstein.core.autofix.daemon.tick_once` directly.
    """
    return []


def _placeholder_dispatch_hook(
    *,
    goal: str,
    model: str,
    effort: str,
    repo: str,
    head_branch: str,
    allow_force_push: bool,
    cost_cap_usd: float,
) -> DispatchResult:
    """Default :class:`DispatchHook` used until the network wiring lands.

    Returns a deterministic ``failed`` result so a developer who
    starts the daemon by accident sees a clear "not yet wired"
    message instead of silently-doing-nothing.
    """
    del goal, model, effort, head_branch, allow_force_push  # unused
    return DispatchResult(
        success=False,
        commit_sha="",
        cost_usd=0.0,
        message=(
            f"autofix dispatch hook not yet wired for {repo} "
            f"(cost_cap=${cost_cap_usd:.2f}); install a hook via the "
            "Python API to enable real attempts."
        ),
    )


def _build_default_dispatcher(workdir: Path) -> Dispatcher:
    """Construct a dispatcher backed by the live audit chain.

    A no-op action adapter is supplied so the placeholder dispatcher
    never tries to hit GitHub.
    """

    class _NoopActions:
        """Best-effort no-op adapter used by the placeholder hook."""

        def post_comment(self, repo: str, pr_number: int, body: str) -> None:
            return

        def add_label(self, repo: str, pr_number: int, label: str) -> None:
            return

        def remove_label(self, repo: str, pr_number: int, label: str) -> None:
            return

    audit = AuditLog(audit_dir=workdir / ".sdd" / "audit")
    return Dispatcher(
        audit=audit,
        action_adapter=_NoopActions(),
        dispatch_hook=_placeholder_dispatch_hook,
        attempt_counter=AttemptCounter(),
    )


def _format_status_line(record: dict[str, object]) -> str:
    """Render a single status JSONL record for human consumption."""
    repo = str(record.get("repo", "?"))
    pr = record.get("pr_number", "?")
    outcome = str(record.get("outcome", "?"))
    classifier = str(record.get("classifier", "?"))
    cost = float(record.get("cost_usd", 0.0) or 0.0)
    attempt_index = record.get("attempt_index", "?")
    return (
        f"{repo}#{pr}  attempt={attempt_index}  "
        f"outcome={outcome}  classifier={classifier}  cost=${cost:.4f}"
    )


def _filter_repos(config: AutofixConfig, repo_filter: tuple[str, ...]) -> set[str] | None:
    """Build the per-tick repo allow-list.

    Returns ``None`` when no filter was supplied (i.e. all repos in
    the config participate); otherwise a set of explicit repo names.
    """
    if not repo_filter:
        return None
    declared = {repo.name for repo in config.repos}
    extras = set(repo_filter) - declared
    if extras:
        # Surface unknown repos so an operator knows the filter
        # did not match anything in the config file.
        click.echo(
            "Warning: --repo arguments not in the config file: " + ", ".join(sorted(extras)),
            err=True,
        )
    return set(repo_filter)


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group("autofix")
def autofix_group() -> None:
    """Auto-repair CI failures on Bernstein-opened pull requests."""


@autofix_group.command("start")
@click.option(
    "--repo",
    "repo_filter",
    multiple=True,
    help="Only watch the named repo(s); repeat to allow several.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override the autofix.toml location.",
)
@click.option(
    "--foreground",
    is_flag=True,
    default=False,
    help="Run the daemon in the current process instead of forking.",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run a single tick and exit (foreground mode).",
)
def start_cmd(
    repo_filter: tuple[str, ...],
    config_path: Path | None,
    foreground: bool,
    once: bool,
) -> None:
    """Start the autofix daemon.

    By default the command exits as soon as the daemon process is
    spawned.  Pass ``--foreground`` to keep the daemon attached
    (useful when running under systemd / launchd).
    """
    workdir = _resolve_workdir()
    try:
        config = load_config(config_path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if not config.repos and not repo_filter:
        raise click.ClickException(
            "autofix.toml declares no [[repo]] entries and no --repo override "
            "was supplied; nothing to watch."
        )

    repos = _filter_repos(config, repo_filter)
    dispatcher = _build_default_dispatcher(workdir)

    if foreground or once:
        try:
            ticks = daemon_start(
                config=config,
                dispatcher=dispatcher,
                failing_source=_placeholder_failing_source,  # type: ignore[arg-type]
                workdir=workdir,
                extra_repo_filter=repos,
                iterations=1 if once else None,
            )
        except DaemonAlreadyRunningError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"autofix daemon completed after {ticks} tick(s).")
        return

    pid = _double_fork_daemon(
        config=config,
        dispatcher_workdir=workdir,
        repos=repos,
        config_path=config_path,
    )
    click.echo(f"autofix daemon started (pid {pid}).")


def _double_fork_daemon(
    *,
    config: AutofixConfig,  # kept for future wiring
    dispatcher_workdir: Path,
    repos: set[str] | None,
    config_path: Path | None,
) -> int:
    """Fork a child that re-execs ``bernstein autofix start --foreground``.

    The double-fork pattern (parent → child → grand-child) ensures
    the daemon has no controlling terminal and is reparented to
    PID 1.  The exact arguments are passed through so the child
    re-loads the same configuration.
    """
    del config  # placeholder hook does not consume the config dict
    pid = os.fork()
    if pid != 0:
        # Parent: wait for the intermediate child to exit so we know
        # the grandchild is detached.
        os.waitpid(pid, 0)
        # The grandchild writes its own pid file; read it back so
        # the operator sees the right number.
        time.sleep(0.1)
        return _safe_pid(dispatcher_workdir)

    # Intermediate child — fork again and exit.
    if os.fork() != 0:
        os._exit(0)

    # Grandchild: detach from controlling terminal.
    os.setsid()
    os.chdir(str(dispatcher_workdir))

    # Re-exec the CLI in foreground mode to avoid carrying parent
    # state into the long-running process.
    args = [sys.executable, "-m", "bernstein", "autofix", "start", "--foreground"]
    if config_path is not None:
        args.extend(["--config", str(config_path)])
    if repos:
        for name in sorted(repos):
            args.extend(["--repo", name])
    os.execvp(args[0], args)
    return 0  # pragma: no cover — execvp does not return on success


def _safe_pid(workdir: Path) -> int:
    """Read the daemon pid file with retries (handles a slow start)."""
    from bernstein.core.autofix.daemon import _read_pid  # local to avoid cycle

    for _ in range(10):
        pid = _read_pid(workdir)
        if pid > 0:
            return pid
        time.sleep(0.05)
    return _read_pid(workdir)


@autofix_group.command("stop")
@click.option(
    "--timeout",
    "timeout_seconds",
    type=float,
    default=10.0,
    show_default=True,
    help="Seconds to wait for the daemon to exit cleanly.",
)
def stop_cmd(timeout_seconds: float) -> None:
    """Stop the running autofix daemon."""
    workdir = _resolve_workdir()
    try:
        pid = daemon_stop(workdir, timeout_seconds=timeout_seconds)
    except DaemonNotRunningError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"autofix daemon stopped (pid {pid}).")


@autofix_group.command("status")
@click.option(
    "--watch",
    is_flag=True,
    default=False,
    help="Tail dispatched attempts as they arrive.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of text.",
)
@click.option(
    "--limit",
    type=int,
    default=20,
    show_default=True,
    help="Number of recent attempts to show.",
)
def status_cmd(watch: bool, as_json: bool, limit: int) -> None:
    """Print daemon status + the most recent attempts."""
    workdir = _resolve_workdir()
    snapshot = read_status(workdir)
    attempts = recent_attempts(workdir, limit=limit)

    if as_json:
        payload = {
            "running": snapshot.running,
            "pid": snapshot.pid,
            "started_at": snapshot.started_at,
            "last_tick_at": snapshot.last_tick_at,
            "recent_attempts": attempts,
        }
        click.echo(json.dumps(payload, sort_keys=True))
        return

    state = "running" if snapshot.running else "stopped"
    click.echo(f"autofix daemon: {state} (pid={snapshot.pid or '-'})")
    if snapshot.last_tick_at:
        click.echo(f"last tick:      {time.ctime(snapshot.last_tick_at)}")
    click.echo("")
    click.echo("Recent attempts (newest first):")
    if not attempts:
        click.echo("  (none yet)")
    for record in attempts:
        click.echo(f"  {_format_status_line(record)}")

    if not watch:
        return

    # Naive tail: poll the JSONL file once per second and print new
    # entries.  The implementation is deliberately simple — this is
    # an operator console, not a high-throughput sink.
    seen = {str(a.get("attempt_id")) for a in attempts}
    try:
        while True:
            time.sleep(1.0)
            for fresh in reversed(recent_attempts(workdir, limit=limit)):
                aid = str(fresh.get("attempt_id"))
                if aid in seen:
                    continue
                seen.add(aid)
                click.echo(f"  {_format_status_line(fresh)}")
    except KeyboardInterrupt:
        return


@autofix_group.command("attach")
@click.option(
    "--limit",
    type=int,
    default=200,
    show_default=True,
    help="Maximum number of past attempts to print before tailing.",
)
def attach_cmd(limit: int) -> None:
    """Stream the daemon's status log to stdout.

    ``attach`` is the resume-token handoff defined by op-005: it
    rejoins a daemon-started session from any terminal by replaying
    the JSONL status log and then tailing it for new entries.
    """
    workdir = _resolve_workdir()
    snapshot = read_status(workdir)
    if not snapshot.running:
        click.echo("autofix daemon is not running; printing the last N entries.")

    attempts = recent_attempts(workdir, limit=limit)
    for record in reversed(attempts):
        click.echo(json.dumps(record, sort_keys=True))

    if not snapshot.running:
        return

    seen = {str(a.get("attempt_id")) for a in attempts}
    try:
        while True:
            time.sleep(1.0)
            for fresh in reversed(recent_attempts(workdir, limit=limit)):
                aid = str(fresh.get("attempt_id"))
                if aid in seen:
                    continue
                seen.add(aid)
                click.echo(json.dumps(fresh, sort_keys=True))
    except KeyboardInterrupt:
        return
