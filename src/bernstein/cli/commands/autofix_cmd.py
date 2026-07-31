"""``bernstein autofix`` CLI group.

Operator-facing entry points for the autofix daemon - start, stop,
status, attach.  All real logic lives in
:mod:`bernstein.core.autofix.daemon`; this module is a thin click
wrapper that handles argument parsing and stdout formatting.

The CLI deliberately exposes :func:`start` *without* forking by
default - operators are expected to launch the daemon under
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
from typing import cast

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
from bernstein.core.autofix.ladder import (
    CIFailure,
    build_default_ladder,
    load_ladder_settings,
    select_rung,
)
from bernstein.core.autofix.review_router import (
    GhInvocationError,
    ReviewRouter,
    WorktreeRecord,
    WorktreeRegistry,
    default_registry_path,
    emit_jsonl,
    poll_loop,
    resolve_pr_number,
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

    The CLI does not yet ship a network-backed source - that lands in
    a follow-up - so the placeholder returns no candidates and lets
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
    cost = float(cast("float | int | str", record.get("cost_usd", 0.0)) or 0.0)
    attempt_index = record.get("attempt_index", "?")
    return f"{repo}#{pr}  attempt={attempt_index}  outcome={outcome}  classifier={classifier}  cost=${cost:.4f}"


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
            "autofix.toml declares no [[repo]] entries and no --repo override was supplied; nothing to watch."
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

    # Intermediate child - fork again and exit.
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
    return 0  # pragma: no cover - execvp does not return on success


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
    # entries.  The implementation is deliberately simple - this is
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


# ---------------------------------------------------------------------------
# review - poll a PR for review-comment routing
# ---------------------------------------------------------------------------


_REVIEW_TASK_LOG = Path(".sdd") / "runtime" / "autofix-review-tasks.jsonl"


@autofix_group.command("review")
@click.option(
    "--pr",
    "pr_number",
    type=int,
    default=None,
    help="PR number to watch (overrides env / git config).",
)
@click.option(
    "--repo",
    "repo_slug",
    default="",
    help="GitHub owner/name slug; passed to ``gh pr view --repo``.",
)
@click.option(
    "--poll-seconds",
    type=float,
    default=60.0,
    show_default=True,
    help="Seconds to sleep between polls.",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run a single poll and exit.",
)
@click.option(
    "--task-log",
    "task_log_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override the JSONL path the router writes structured tasks to.",
)
def review_cmd(
    pr_number: int | None,
    repo_slug: str,
    poll_seconds: float,
    once: bool,
    task_log_path: Path | None,
) -> None:
    """Poll a PR's review threads and emit structured tasks for new "request changes" comments.

    The command resolves which PR to watch in the following order:

    1. ``--pr`` argument.
    2. ``BERNSTEIN_REVIEW_PR_NUMBER`` environment variable.
    3. ``git config bernstein.spawn-pr``.

    Tasks are appended as JSONL to ``.sdd/runtime/autofix-review-tasks.jsonl``
    relative to the current workspace (override with ``--task-log``).
    Each task carries the file, line range, comment body, reviewer login,
    diff hunk and permalink - enough for the spawning agent to address
    the comment without re-fetching from GitHub.
    """
    workdir = _resolve_workdir()
    resolved_pr = resolve_pr_number(explicit=pr_number, workdir=workdir)
    if resolved_pr is None:
        raise click.ClickException(
            "no PR specified - pass --pr, set BERNSTEIN_REVIEW_PR_NUMBER, or set ``git config bernstein.spawn-pr``."
        )

    log_path = task_log_path or (workdir / _REVIEW_TASK_LOG)
    sink = emit_jsonl(log_path)
    router = ReviewRouter(
        pr_number=resolved_pr,
        task_sink=sink,
        repo=repo_slug,
    )

    # Surface the spawning agent's worktree (if registered) so an
    # operator running ``review --once`` from any directory still
    # sees which workspace the eventual dispatcher will resume.
    record = WorktreeRegistry(default_registry_path(workdir)).lookup(resolved_pr)
    if record is not None:
        click.echo(
            f"review_router: PR #{resolved_pr} maps to worktree {record.worktree_path}"
            + (f" (run_id={record.run_id})" if record.run_id else "")
        )

    if once:
        try:
            outcome = router.poll_once()
        except GhInvocationError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"review_router: PR #{resolved_pr} emitted {len(outcome.tasks)} task(s); "
            f"skipped {outcome.skipped_seen} already-seen comment(s); log={log_path}"
        )
        return

    click.echo(f"review_router: watching PR #{resolved_pr} every {poll_seconds:.1f}s; log={log_path}")
    try:
        polls = poll_loop(router, poll_seconds=poll_seconds)
    except KeyboardInterrupt:
        click.echo("review_router: interrupted.")
        return
    click.echo(f"review_router: completed after {polls} poll(s).")


# ---------------------------------------------------------------------------
# review-register / review-resolve - operator-facing worktree registry
# ---------------------------------------------------------------------------


@autofix_group.command("review-register")
@click.option(
    "--pr",
    "pr_number",
    type=int,
    required=True,
    help="PR number opened by the spawning agent.",
)
@click.option(
    "--worktree",
    "worktree_path",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Absolute path to the worktree the agent used.",
)
@click.option(
    "--run-id",
    "run_id",
    default="",
    help="Bernstein run id that opened the PR (optional, recorded for audit).",
)
@click.option(
    "--repo",
    "repo_slug",
    default="",
    help="GitHub owner/name slug (optional).",
)
def review_register_cmd(
    pr_number: int,
    worktree_path: Path,
    run_id: str,
    repo_slug: str,
) -> None:
    """Persist a ``pr_number -> worktree_path`` mapping.

    Call this immediately after ``bernstein pr`` opens a review-ready
    PR so a future ``bernstein autofix review`` invocation can locate
    the spawning agent's workspace, even after a host restart.
    """
    workdir = _resolve_workdir()
    registry = WorktreeRegistry(default_registry_path(workdir))
    try:
        registry.register(
            WorktreeRecord(
                pr_number=pr_number,
                worktree_path=str(worktree_path),
                run_id=run_id,
                repo=repo_slug,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"review_router: registered PR #{pr_number} -> {worktree_path}")


@autofix_group.command("review-resolve")
@click.option(
    "--pr",
    "pr_number",
    type=int,
    required=True,
    help="PR number to look up.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of plain text.",
)
def review_resolve_cmd(pr_number: int, as_json: bool) -> None:
    """Print the registered worktree path for a PR (exit non-zero if missing)."""
    workdir = _resolve_workdir()
    record = WorktreeRegistry(default_registry_path(workdir)).lookup(pr_number)
    if record is None:
        if as_json:
            click.echo(json.dumps({"pr_number": pr_number, "found": False}, sort_keys=True))
        else:
            click.echo(f"review_router: no registration for PR #{pr_number}", err=True)
        sys.exit(1)
    if as_json:
        payload = {
            "pr_number": pr_number,
            "found": True,
        } | record.to_payload()
        click.echo(json.dumps(payload, sort_keys=True))
        return
    line = f"PR #{record.pr_number}  worktree={record.worktree_path}"
    if record.run_id:
        line += f"  run_id={record.run_id}"
    if record.repo:
        line += f"  repo={record.repo}"
    click.echo(line)


# ---------------------------------------------------------------------------
# ladder - RFC #1415 escalation ladder, dry-run only in MVP
# ---------------------------------------------------------------------------


def _split_csv(raw: str) -> tuple[str, ...]:
    """Split a comma-separated CLI arg into a clean tuple."""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _build_failure_from_payload(
    *,
    repo: str,
    pr_number: int,
    payload_path: Path | None,
    log_excerpt: str,
    failing_files: str,
    pr_touched_files: str,
    diff_line_count: int,
    signature: str,
) -> CIFailure:
    """Compose a :class:`CIFailure` from CLI flags or a JSON payload.

    The JSON payload (``--payload``) wins over the discrete flags so
    operators can drop a captured failure into the simulator without
    transcribing each field.
    """
    base: dict[str, object] = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": "",
        "run_id": "",
        "failing_files": _split_csv(failing_files),
        "pr_touched_files": _split_csv(pr_touched_files),
        "log_excerpt": log_excerpt,
        "diff_line_count": diff_line_count,
        "signature": signature,
    }
    if payload_path is not None:
        try:
            extra = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise click.ClickException(f"failed to read --payload {payload_path}: {exc}") from exc
        if not isinstance(extra, dict):
            raise click.ClickException("--payload must contain a JSON object")
        for key in (
            "repo",
            "pr_number",
            "head_sha",
            "run_id",
            "failing_files",
            "pr_touched_files",
            "log_excerpt",
            "diff_line_count",
            "signature",
        ):
            if key not in extra:
                continue
            value = extra[key]
            if key in {"failing_files", "pr_touched_files"} and isinstance(value, list):
                base[key] = tuple(str(item) for item in value)
            else:
                base[key] = value
    return CIFailure(
        repo=str(base["repo"]),
        pr_number=int(cast("int | str", base["pr_number"])),
        head_sha=str(base.get("head_sha", "")),
        run_id=str(base.get("run_id", "")),
        failing_files=tuple(base["failing_files"]),  # type: ignore[arg-type]
        pr_touched_files=tuple(base["pr_touched_files"]),  # type: ignore[arg-type]
        log_excerpt=str(base.get("log_excerpt", "")),
        diff_line_count=int(cast("int | str", base.get("diff_line_count", 0))),
        signature=str(base.get("signature", "")),
    )


@autofix_group.command("ladder")
@click.option(
    "--pr",
    "pr_number",
    type=int,
    required=True,
    help="PR number the failure belongs to.",
)
@click.option(
    "--repo",
    "repo_slug",
    default="",
    help="GitHub owner/name slug; defaults to empty string for dry-run.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Required: show the rung that would fire without acting.",
)
@click.option(
    "--payload",
    "payload_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Optional JSON payload describing the synthetic CIFailure.",
)
@click.option(
    "--log-excerpt",
    "log_excerpt",
    default="",
    help="Failing-log excerpt used by the rung-0 detector.",
)
@click.option(
    "--failing-files",
    "failing_files",
    default="",
    help="Comma-separated list of files the CI reported as failing.",
)
@click.option(
    "--pr-touched-files",
    "pr_touched_files",
    default="",
    help="Comma-separated list of files the PR's diff touches.",
)
@click.option(
    "--diff-line-count",
    "diff_line_count",
    type=int,
    default=0,
    help="Approximate diff line count; used by the rung-1 detector.",
)
@click.option(
    "--signature",
    "signature",
    default="",
    help="Stable failure signature persisted to the audit trail.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override the bernstein.yaml location (defaults to ./bernstein.yaml).",
)
def ladder_cmd(
    pr_number: int,
    repo_slug: str,
    dry_run: bool,
    payload_path: Path | None,
    log_excerpt: str,
    failing_files: str,
    pr_touched_files: str,
    diff_line_count: int,
    signature: str,
    config_path: Path | None,
) -> None:
    """Self-driving CI escalation ladder (RFC #1415).

    The MVP only supports ``--dry-run`` mode. Pass the failure shape
    via discrete flags or via ``--payload <file.json>``. The command
    selects the rung that would fire under the current
    ``bernstein.yaml: autofix`` configuration and prints the result.
    No side effects: no patch is applied, no comment is posted.
    """
    if not dry_run:
        raise click.ClickException(
            "ladder MVP only supports --dry-run; rerun with --dry-run to see the rung that would fire."
        )

    failure = _build_failure_from_payload(
        repo=repo_slug,
        pr_number=pr_number,
        payload_path=payload_path,
        log_excerpt=log_excerpt,
        failing_files=failing_files,
        pr_touched_files=pr_touched_files,
        diff_line_count=diff_line_count,
        signature=signature,
    )

    settings = load_ladder_settings(config_path)
    ladder = build_default_ladder(
        apply_lint_patch=lambda _f: (False, "", "dry-run: lint patch not applied"),
        post_comment=lambda _repo, _pr, _body: None,
    )
    selection = select_rung(ladder, failure, cost_cap_per_pr=settings.cost_cap_per_pr_usd)

    click.echo(f"autofix ladder (dry-run) for {failure.repo or '(no repo)'}#{failure.pr_number}")
    click.echo(f"  feature flag:       enabled={settings.enabled}")
    click.echo(f"  cost_cap_per_pr:    ${settings.cost_cap_per_pr_usd:.2f}")
    if selection.rung is None:
        click.echo("  rung:               (no match)")
    else:
        click.echo(f"  rung:               {selection.rung.rung_id}")
        click.echo(f"  rung description:   {selection.rung.description}")
        click.echo(f"  rung cost cap:      ${selection.rung.cost_cap_usd:.2f}")
    click.echo(f"  accepted:           {selection.accepted}")
    click.echo(f"  reason:             {selection.reason}")
    if not settings.enabled:
        click.echo("  note:               autofix.ladder.enabled is false; the daemon would skip this match.")
