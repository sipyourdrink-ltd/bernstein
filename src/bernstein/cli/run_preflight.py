"""Preflight cost estimation and runtime warnings for Bernstein runs."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from bernstein.cli.helpers import (
    SERVER_URL,
    console,
    find_seed_file,
)
from bernstein.cli.run import render_run_summary_from_dict
from bernstein.cli.ui import make_console
from bernstein.core.cost import estimate_run_cost
from bernstein.core.cost.preflight import CostBand, compute_band, format_band
from bernstein.core.plan_loader import load_plan_from_yaml
from bernstein.core.runtime_state import directory_size_bytes

if TYPE_CHECKING:
    from rich.console import Console

    from bernstein.core.config.seed import SeedConfig

logger = logging.getLogger(__name__)


def validate_seed_or_exit(seed_file: str | None) -> SeedConfig | None:
    """Parse and validate the seed file with the same rules the real run uses.

    Resolves the seed path (falling back to :func:`find_seed_file` when
    ``seed_file`` is ``None``) and parses it through
    :func:`bernstein.core.seed.parse_seed` -- the single shared validation
    boundary. A :class:`~bernstein.core.seed.SeedError` is surfaced as the
    same structured CLI error the real run raises and aborts the process, so
    ``--dry-run`` can no longer report success on a seed the run would reject
    (issue #2785).

    Args:
        seed_file: Explicit seed path, or ``None`` to auto-discover one.

    Returns:
        The parsed :class:`SeedConfig` when a seed file exists and validates,
        or ``None`` when no seed file is present (inline-goal and empty-backlog
        modes validate elsewhere).

    Raises:
        SystemExit: When the seed file exists but fails validation.
    """
    from bernstein.core.config.seed import SeedError, parse_seed

    seed_path = Path(seed_file) if seed_file is not None else find_seed_file()
    if seed_path is None or not seed_path.exists():
        return None
    try:
        return parse_seed(seed_path)
    except SeedError as exc:
        from bernstein.cli.utils.errors import seed_parse_error

        seed_parse_error(exc).print()
        raise SystemExit(1) from exc


#: When this module was imported, which is when the CLI process started.
#: Used to ignore merge-refusal journal entries left over from previous runs
#: when surfacing refusals in the end-of-run summary.
_CLI_RUN_EPOCH = time.time()

# ---------------------------------------------------------------------------
# Post-run summary helper
# ---------------------------------------------------------------------------


def _abort_if_default_branch_merge_target(workdir: Path) -> None:
    """Abort before any agent spawns when merges would land on the default branch.

    The spawner merge guard (``spawner_merge._run_merge_and_push``) refuses to
    merge agent work onto the repository's protected default branch. When the
    run starts with that branch checked out and the override env var unset,
    every agent would do its work and then have it silently discarded at
    merge time (gh-2756). Detect that state up front and abort with the
    remedy instead.

    Only wired into run modes that merge agent work back into the checked-out
    branch; ``--dry-run`` and ``--plan-only`` never reach this check.

    Args:
        workdir: Repository root the run would merge agent work into.

    Raises:
        SystemExit: When the checked-out branch is a protected default branch
            and ``BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH`` is not set.
    """
    from bernstein.core.agents.spawner_merge import (
        ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH,
        _allow_merge_to_default_branch,
    )
    from bernstein.core.git_ops import current_branch, protected_default_branches

    branch = current_branch(workdir)
    if branch is None:
        # Detached HEAD or not a git repo: mirror the merge guard, which only
        # refuses when a named protected branch is checked out.
        return
    if branch not in protected_default_branches(workdir):
        return
    if _allow_merge_to_default_branch():
        return
    console.print(
        f"[bold red]Refusing to start:[/bold red] the checked-out branch {branch!r} is the "
        "repository default branch, so every agent's work would be refused by the merge "
        "guard and discarded instead of merged."
    )
    console.print(
        "Fix: check out a working branch first (git checkout -b <branch>), or set "
        f"{ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH}=1 to explicitly allow merging to the default branch."
    )
    raise SystemExit(1)


def _surface_merge_refusals(workdir: Path, *, since_ts: float, console: Console) -> None:
    """Print a loud warning for agent merges the merge guard refused this run.

    The spawner merge guard refuses to land agent work on the repository's
    protected default branch and records each refusal to
    ``.sdd/runtime/refused_merges.jsonl``. Without this warning the refusal
    is only visible in the spawner log, so the run ends looking clean while
    the work was discarded (gh-2756).

    Args:
        workdir: Repository root of the run.
        since_ts: Only refusals recorded at or after this timestamp are
            shown, filtering out journal entries from previous runs.
        console: Console to print the warning to.
    """
    from bernstein.core.agents.spawner_merge import ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH
    from bernstein.core.quality.retrospective import read_merge_refusals

    refusals = read_merge_refusals(workdir / ".sdd" / "runtime", since_ts=since_ts)
    if not refusals:
        return
    branches = sorted({r.branch for r in refusals if r.branch})
    branch_note = f" onto default branch {', '.join(repr(b) for b in branches)}" if branches else ""
    console.print(
        f"[bold red]Merge refused:[/bold red] agent work from {len(refusals)} session(s) was NOT "
        f"merged{branch_note} and was discarded (details: .sdd/runtime/refused_merges.jsonl)."
    )
    console.print(
        "Check out a working branch first (git checkout -b <branch>), or set "
        f"{ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH}=1 and re-run."
    )


def _show_run_summary() -> None:
    """Fetch final status from the task server and render a summary.

    Silently skips the status table when the server is unreachable (e.g.
    already stopped). Merge refusals recorded during this run are surfaced
    even then -- a run whose work was discarded must not end with a clean,
    silent summary (gh-2756).
    """
    from bernstein.cli.helpers import server_get

    force_no_color = not sys.stdout.isatty()
    con = make_console(no_color=force_no_color)
    data = server_get("/status")
    if data is not None:
        render_run_summary_from_dict(data, console=con)
    _surface_merge_refusals(Path.cwd(), since_ts=_CLI_RUN_EPOCH, console=con)


def _drain_completed_backlog_files() -> None:
    """Move backlog files for terminal tasks from ``claimed/`` to ``closed/``.

    This is the post-run safety-net for the periodic sync tick: if the
    orchestrator stops before its next sync, completed tickets can stay
    pinned in ``.sdd/backlog/claimed/``.  We invoke the same logic the
    sync loop uses, but only the move step (no task creation), and we
    swallow any exception so a cleanup failure never aborts shutdown.

    Safe to call when the task server is already gone; in that case the
    httpx connection raises, we log at debug, and return.
    """
    import httpx

    from bernstein.core.sync import SyncResult, _move_completed_files

    workdir = Path.cwd()
    backlog_open = workdir / ".sdd" / "backlog" / "open"
    backlog_issues = workdir / ".sdd" / "backlog" / "issues"
    if not (workdir / ".sdd" / "backlog" / "claimed").exists():
        return

    result = SyncResult()
    try:
        with httpx.Client(timeout=5.0) as client:
            _move_completed_files(workdir, client, SERVER_URL, backlog_open, backlog_issues, result)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Post-run claimed/ drain failed: %s: %s", type(exc).__name__, exc)


@dataclass(frozen=True)
class RunCostEstimate:
    """Preflight cost estimate for a pending run.

    Attributes:
        task_count: Number of tasks the run will spawn, or ``None`` when the
            count is not yet known (inline goal that the manager agent plans
            after startup). ``low_usd``/``high_usd`` are then per-task rates.
        model: Display label for the model (``"adapter/model"`` or model).
        low_usd: Legacy single-point low estimate (kept for back-compat).
        high_usd: Legacy single-point high estimate (kept for back-compat).
        band: Optional calibrated p50/p90 band. Populated for new callers;
            legacy callers can leave it unset.
    """

    task_count: int | None
    model: str
    low_usd: float
    high_usd: float
    band: CostBand | None = None


def _estimate_task_count(workdir: Path, plan_file: Path | None, goal: str | None) -> int | None:
    """Count tasks from the plan file or the synced backlog.

    Returns:
        The task count when it is knowable (explicit plan file or non-empty
        backlog), or ``None`` when planning has not happened yet (inline
        goal, unreadable plan file, empty backlog). ``None`` means unknown;
        callers must not substitute a made-up number for display.
    """
    if plan_file is not None:
        try:
            return max(1, len(load_plan_from_yaml(plan_file)))
        except Exception:
            return None
    if goal is not None:
        return None
    count = 0
    for subdir in ("open", "issues"):
        backlog_dir = workdir / ".sdd" / "backlog" / subdir
        if backlog_dir.exists():
            count += len(list(backlog_dir.glob("*.md")))
            count += len(list(backlog_dir.glob("*.yaml")))
            count += len(list(backlog_dir.glob("*.yml")))
    return count if count > 0 else None


def _resolve_model_and_cli(
    seed_file: str | None,
    model_override: str | None,
    seed: SeedConfig | None = None,
) -> tuple[str, str, str]:
    """Resolve model, CLI adapter, and dominant role from seed or defaults.

    Args:
        seed_file: Explicit seed path, or ``None`` to auto-discover one.
        model_override: Explicit ``--model`` override; short-circuits seed
            inspection when set.
        seed: A seed already parsed by the shared validation boundary
            (:func:`validate_seed_or_exit`). When supplied it is used
            directly, so the estimate reflects the seed's effective default
            model instead of falling back to the sonnet heuristic on a
            re-parse failure (issue #2785).

    Returns:
        Tuple of ``(model, cli, role)``. ``role`` defaults to ``"backend"``
        when the seed does not specify a role policy.
    """
    est_model = model_override or "sonnet"
    est_cli = "claude"
    est_role = "backend"
    if model_override is not None:
        return est_model, est_cli, est_role

    if seed is None:
        seed_path = Path(seed_file) if seed_file is not None else find_seed_file()
        if seed_path is None or not seed_path.exists():
            return est_model, est_cli, est_role
        try:
            from bernstein.core.config.seed import parse_seed

            seed = parse_seed(seed_path)
        except Exception:
            return "sonnet", est_cli, est_role

    try:
        from bernstein.core.cost.cost import _model_cost

        if seed.model:
            est_model = seed.model
        if seed.role_model_policy:
            # Pick the role with the most expensive model so the preflight
            # estimate is an upper bound on actual spend.  Previously the
            # loop took only the first dict entry and ``break``-ed, which
            # is non-deterministic when role insertion order varies and,
            # worse, can under-report cost by orders of magnitude when a
            # cheap role (e.g. qa on gemini) shadows an expensive role
            # (e.g. backend on opus) in the same seed.
            best_role = est_role
            best_cli = est_cli
            best_model = est_model
            best_cost = _model_cost(est_model)
            for _role, _policy in seed.role_model_policy.items():
                if not isinstance(_role, str):
                    continue
                role_cli = _policy.get("cli", est_cli)
                role_model = _policy.get("model", est_model)
                role_cost = _model_cost(role_model)
                if role_cost > best_cost:
                    best_role = _role
                    best_cli = role_cli
                    best_model = role_model
                    best_cost = role_cost
            est_role = best_role
            est_cli = best_cli
            est_model = best_model
        if seed.cli and seed.cli != "auto":
            est_cli = seed.cli
    except Exception:
        est_model = "sonnet"
    return est_model, est_cli, est_role


_FREE_ADAPTERS = frozenset(("qwen", "gemini", "ollama"))


def _estimate_run_preview(
    *,
    workdir: Path,
    plan_file: Path | None,
    goal: str | None,
    seed_file: str | None,
    model_override: str | None,
    seed: SeedConfig | None = None,
) -> RunCostEstimate:
    """Estimate run cost before bootstrapping the orchestrator.

    Computes both the legacy single-point heuristic (``low_usd``/``high_usd``,
    kept for back-compat) and the calibrated ``CostBand`` (``band``).
    The calibrated band reads the last 50 records of the same
    ``(role, adapter)`` pair from ``.sdd/metrics/cost.jsonl``; cold-start
    falls back to the legacy heuristic and the band is flagged
    ``cold_start=True``.

    Args:
        workdir: Repository root.
        plan_file: Optional explicit YAML plan file.
        goal: Optional inline goal.
        seed_file: Optional seed path override.
        model_override: Optional CLI ``--model`` override.
        seed: Seed already parsed by the shared validation boundary. When
            supplied, the estimate reflects the seed's effective default
            model rather than re-parsing (issue #2785).

    Returns:
        Cost estimate using the best available task count and model hint.
    """
    est_task_count = _estimate_task_count(workdir, plan_file, goal)
    # Unknown count: compute a per-task rate (count of 1) but keep
    # ``task_count=None`` so display code says "unknown" instead of a number.
    billable_count = est_task_count if est_task_count is not None else 1
    est_model, est_cli, est_role = _resolve_model_and_cli(seed_file, model_override, seed=seed)

    if est_cli in _FREE_ADAPTERS:
        low_usd, high_usd = 0.0, 0.0
        band = CostBand(
            p50=0.0,
            p90=0.0,
            samples=0,
            cold_start=False,
            role=est_role,
            adapter=est_cli,
            model=est_model,
        )
    else:
        low_usd, high_usd = estimate_run_cost(billable_count, est_model)
        band = compute_band(
            role=est_role,
            adapter=est_cli,
            model=est_model,
            task_count=billable_count,
            metrics_dir=workdir / ".sdd" / "metrics",
        )
    display_model = f"{est_cli}/{est_model}" if est_cli != "claude" else est_model
    return RunCostEstimate(
        task_count=est_task_count,
        model=display_model,
        low_usd=low_usd,
        high_usd=high_usd,
        band=band,
    )


def _emit_preflight_runtime_warnings(
    *,
    workdir: Path,
    estimate: RunCostEstimate,
    auto_approve: bool,
    quiet: bool,
    plan_approval_follows: bool = False,
    budget_cap: float | None = None,
) -> None:
    """Show startup cost and disk-usage warnings before execution.

    Args:
        workdir: Repository root.
        estimate: Cost estimate computed from local context.
        auto_approve: Whether confirmation prompts are disabled.
        quiet: Whether normal startup output is suppressed.
        plan_approval_follows: When True, a plan-approval prompt that
            already shows cost will follow, so we skip the duplicate
            confirmation here.
        budget_cap: Optional preflight ceiling in USD. When the p90 of the
            calibrated band exceeds this value, the spawn is aborted with
            ``SystemExit(1)``. ``None`` (default) disables the check.

    Raises:
        SystemExit: When the operator declines a high-cost run or when
            ``budget_cap`` is exceeded.
    """
    sdd_dir = workdir / ".sdd"
    disk_usage_gb = directory_size_bytes(sdd_dir) / (1024**3)
    band = estimate.band
    if not quiet:
        # Only print a count that came from the plan/backlog; when the count
        # is unknown (planning happens after startup) say so explicitly
        # instead of substituting a made-up number.
        if estimate.task_count is not None:
            basis = f"based on {estimate.task_count} task(s) at {estimate.model} pricing"
        else:
            basis = f"per task at {estimate.model} pricing, task count not yet planned"
        if band is not None:
            console.print(f"[bold yellow]{format_band(band)}[/bold yellow]")
            samples_note = (
                f"{band.samples} historical sample(s)" if not band.cold_start else "no history yet - using heuristic"
            )
            console.print(f"[dim]{basis}, {samples_note}[/dim]")
        else:
            console.print(
                f"[bold yellow]Estimated cost:[/bold yellow] ${estimate.low_usd:.2f}-${estimate.high_usd:.2f} {basis}"
            )
        if disk_usage_gb >= 1.0:
            console.print(
                "[yellow]Warning:[/yellow] "
                f".sdd/ is using {disk_usage_gb:.2f} GB. "
                "Run [bold]bernstein cleanup[/bold] if stale worktrees or logs are accumulating."
            )

    # --budget-cap: abort before spawn when p90 exceeds the operator's
    # ceiling.  Honours auto-approve so CI can opt out of the prompt;
    # when no band is available (e.g. legacy callers) we compare against
    # ``high_usd`` so the contract still holds.
    if budget_cap is not None and budget_cap > 0.0:
        p90 = band.p90 if band is not None else estimate.high_usd
        if p90 > budget_cap:
            console.print(
                f"[bold red]Budget cap exceeded:[/bold red] p90 ${p90:.2f} > cap ${budget_cap:.2f}. Aborting spawn."
            )
            raise SystemExit(1)

    # Cost confirmation is skipped when the plan approval prompt follows
    # (it already shows cost and asks Y/N - no need to ask twice).
    if (
        not auto_approve
        and not plan_approval_follows
        and estimate.high_usd > 10.0
        and not click.confirm(
            f"Warning: estimated cost may reach ${estimate.high_usd:.2f}. Continue?",
            default=True,
        )
    ):
        raise SystemExit(1)


@contextlib.contextmanager
def _quiet_bootstrap_console(enabled: bool) -> Any:
    """Suppress bootstrap Rich output while leaving the final summary visible.

    Args:
        enabled: When True, redirects bootstrap console writes to an in-memory buffer.

    Yields:
        ``None`` while the bootstrap module uses a muted console.
    """
    if not enabled:
        yield
        return

    from rich.console import Console

    import bernstein.core.bootstrap as bootstrap_module

    original_console = bootstrap_module.console
    bootstrap_module.console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    try:
        yield
    finally:
        bootstrap_module.console = original_console


def _make_profile_ctx(profile: bool, workdir: Path) -> contextlib.AbstractContextManager[Any]:
    """Return a ProfilerSession context manager, or a no-op if profiling is disabled.

    Args:
        profile: Whether profiling is enabled.
        workdir: Project root directory used to resolve output path.

    Returns:
        A context manager that profiles the wrapped block (or does nothing).
    """
    import contextlib

    if profile:
        from bernstein.core.profiler import ProfilerSession, resolve_profile_output_dir

        return ProfilerSession(resolve_profile_output_dir(workdir))
    return contextlib.nullcontext()


def _finalize_run_output(*, quiet: bool) -> None:
    """Render either the interactive dashboard or the final summary.

    Uses terminal capability detection (TUI-003) to choose between the
    full Textual TUI and a Rich-based fallback for unsupported terminals.

    Cleanup ordering (gh-953): the ``claimed/`` drain is wrapped in a
    ``try/finally`` around the renderer so that a UI crash (e.g. a shape
    mismatch in the run-summary payload) cannot leave completed tickets
    pinned in ``.sdd/backlog/claimed/`` indefinitely.  ``try/finally`` was
    chosen over reordering because (a) it preserves the existing render-then-
    cleanup ordering on the happy path so the summary still reflects the
    pre-drain state, and (b) it keeps cleanup correct even when the renderer
    raises ``SystemExit`` or ``KeyboardInterrupt``.

    Args:
        quiet: When True, wait for quiescence and print only the terminal summary.
    """
    from bernstein.cli.run_bootstrap import _wait_for_run_completion, exec_restart

    try:
        if quiet:
            _wait_for_run_completion()
            _show_run_summary()
            return

        from bernstein.cli.terminal_caps import detect_capabilities

        caps = detect_capabilities()

        if caps.supports_textual:
            try:
                from bernstein.cli.dashboard import BernsteinApp as DashboardApp

                app = DashboardApp()
                with contextlib.suppress(SystemExit):
                    app.run()
                # Hot restart: server+orchestrator already killed by the TUI,
                # re-exec the full `bernstein run` so everything restarts cleanly.
                if getattr(app, "_restart_on_exit", False):
                    exec_restart()
            except Exception:
                # Textual failed at runtime -- fall through to fallback
                _try_fallback_display()
        elif caps.is_tty:
            # TTY but Textual not supported -- use Rich fallback (TUI-003)
            _try_fallback_display()
        else:
            # Non-interactive output detaches from the run immediately, so a
            # spawn refusal in the background orchestrator would never reach
            # the terminal (gh-2744).  Wait briefly for the first spawn
            # outcome and surface a refusal as a non-zero exit.
            from bernstein.cli.run_bootstrap import _await_first_spawn_outcome

            outcome, reason = _await_first_spawn_outcome()
            _show_run_summary()
            if outcome == "refused":
                console.print(f"[red]Run failed before any work started:[/red] {reason}")
                console.print("Details: run 'bernstein status' or read .sdd/runtime/retrospective.md")
                raise SystemExit(1)
            console.print("Run continues in the background (check: bernstein status).")
    finally:
        _drain_completed_backlog_files()


def _try_fallback_display() -> None:
    """Attempt to run the Rich-based fallback display (TUI-003).

    Falls back to the static summary if even Rich Live fails.
    """
    try:
        from bernstein.tui.fallback import FallbackDisplay

        FallbackDisplay().run()
    except Exception:
        _show_run_summary()


def _configure_quality_gate_bypass(
    *,
    goal: str | None,
    seed_file: str | None,
    skip_gate: tuple[str, ...],
    skip_gate_reason: str | None,
) -> None:
    """Validate and export quality-gate bypass settings for the orchestrator."""
    if not skip_gate and not skip_gate_reason:
        os.environ.pop("BERNSTEIN_SKIP_GATES", None)
        os.environ.pop("BERNSTEIN_SKIP_GATE_REASON", None)
        return
    if skip_gate_reason and not skip_gate:
        raise click.UsageError("--skip-gate-reason requires at least one --skip-gate")
    if goal is not None:
        raise click.UsageError("--skip-gate requires a seed file with quality_gates.allow_bypass: true")

    from bernstein.core.seed import SeedError, parse_seed

    seed_path = Path(seed_file) if seed_file is not None else find_seed_file()
    if seed_path is None:
        raise click.UsageError("--skip-gate requires a seed file with quality_gates.allow_bypass: true")

    try:
        seed = parse_seed(seed_path)
    except SeedError as exc:
        raise click.UsageError(str(exc)) from exc

    if seed.quality_gates is None or not seed.quality_gates.allow_bypass:
        raise click.UsageError("quality_gates.allow_bypass must be true to use --skip-gate")

    normalized = sorted({gate.strip() for gate in skip_gate if gate.strip()})
    if not normalized:
        raise click.UsageError("At least one non-empty --skip-gate is required")
    os.environ["BERNSTEIN_SKIP_GATES"] = ",".join(normalized)
    if skip_gate_reason:
        os.environ["BERNSTEIN_SKIP_GATE_REASON"] = skip_gate_reason
    else:
        os.environ.pop("BERNSTEIN_SKIP_GATE_REASON", None)
