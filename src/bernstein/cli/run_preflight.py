"""Preflight cost estimation and runtime warnings for Bernstein runs."""

from __future__ import annotations

import contextlib
import io
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import (
    console,
    find_seed_file,
)
from bernstein.cli.run import render_run_summary_from_dict
from bernstein.cli.ui import make_console
from bernstein.core.cost import estimate_run_cost
from bernstein.core.plan_loader import load_plan_from_yaml
from bernstein.core.runtime_state import directory_size_bytes

# ---------------------------------------------------------------------------
# Post-run summary helper
# ---------------------------------------------------------------------------


def _show_run_summary() -> None:
    """Fetch final status from the task server and render a summary.

    Silently returns if the server is unreachable (e.g. already stopped).
    """
    from bernstein.cli.helpers import server_get

    data = server_get("/status")
    if data is None:
        return
    force_no_color = not sys.stdout.isatty()
    con = make_console(no_color=force_no_color)
    render_run_summary_from_dict(data, console=con)


@dataclass(frozen=True)
class RunCostEstimate:
    """Preflight cost estimate for a pending run."""

    task_count: int
    model: str
    low_usd: float
    high_usd: float


def _estimate_task_count(
    workdir: Path, plan_file: Path | None, goal: str | None
) -> int:
    """Estimate the number of tasks from plan file or backlog."""
    if plan_file is not None:
        try:
            return max(1, len(load_plan_from_yaml(plan_file)))
        except Exception:
            return 5
    if goal is not None:
        return 5
    count = 0
    for subdir in ("open", "issues"):
        backlog_dir = workdir / ".sdd" / "backlog" / subdir
        if backlog_dir.exists():
            count += len(list(backlog_dir.glob("*.md")))
            count += len(list(backlog_dir.glob("*.yaml")))
            count += len(list(backlog_dir.glob("*.yml")))
    return max(1, count)


def _resolve_model_and_cli(
    seed_file: str | None, model_override: str | None
) -> tuple[str, str]:
    """Resolve model and CLI adapter from seed file or defaults."""
    est_model = model_override or "sonnet"
    est_cli = "claude"
    if model_override is not None:
        return est_model, est_cli

    seed_path = Path(seed_file) if seed_file is not None else find_seed_file()
    if seed_path is None or not seed_path.exists():
        return est_model, est_cli

    try:
        from bernstein.core.seed import parse_seed

        seed = parse_seed(seed_path)
        if seed.model:
            est_model = seed.model
        if seed.role_model_policy:
            for _role, _policy in seed.role_model_policy.items():
                if "cli" in _policy:
                    est_cli = _policy["cli"]
                if "model" in _policy:
                    est_model = _policy["model"]
                break
        if seed.cli and seed.cli != "auto":
            est_cli = seed.cli
    except Exception:
        est_model = "sonnet"
    return est_model, est_cli


_FREE_ADAPTERS = frozenset(("qwen", "gemini", "ollama", "tabby"))


def _estimate_run_preview(
    *,
    workdir: Path,
    plan_file: Path | None,
    goal: str | None,
    seed_file: str | None,
    model_override: str | None,
) -> RunCostEstimate:
    """Estimate run cost before bootstrapping the orchestrator.

    Args:
        workdir: Repository root.
        plan_file: Optional explicit YAML plan file.
        goal: Optional inline goal.
        seed_file: Optional seed path override.
        model_override: Optional CLI ``--model`` override.

    Returns:
        Cost estimate using the best available task count and model hint.
    """
    est_task_count = _estimate_task_count(workdir, plan_file, goal)
    est_model, est_cli = _resolve_model_and_cli(seed_file, model_override)

    if est_cli in _FREE_ADAPTERS:
        low_usd, high_usd = 0.0, 0.0
    else:
        low_usd, high_usd = estimate_run_cost(est_task_count, est_model)
    display_model = f"{est_cli}/{est_model}" if est_cli != "claude" else est_model
    return RunCostEstimate(
        task_count=est_task_count,
        model=display_model,
        low_usd=low_usd,
        high_usd=high_usd,
    )


def _emit_preflight_runtime_warnings(
    *,
    workdir: Path,
    estimate: RunCostEstimate,
    auto_approve: bool,
    quiet: bool,
    plan_approval_follows: bool = False,
) -> None:
    """Show startup cost and disk-usage warnings before execution.

    Args:
        workdir: Repository root.
        estimate: Cost estimate computed from local context.
        auto_approve: Whether confirmation prompts are disabled.
        quiet: Whether normal startup output is suppressed.

    Raises:
        SystemExit: When the operator declines a high-cost run.
    """
    sdd_dir = workdir / ".sdd"
    disk_usage_gb = directory_size_bytes(sdd_dir) / (1024**3)
    if not quiet:
        console.print(
            "[bold yellow]Estimated cost:[/bold yellow] "
            f"${estimate.low_usd:.2f}-${estimate.high_usd:.2f} "
            f"based on {estimate.task_count} task(s) at {estimate.model} pricing"
        )
        if disk_usage_gb >= 1.0:
            console.print(
                "[yellow]Warning:[/yellow] "
                f".sdd/ is using {disk_usage_gb:.2f} GB. "
                "Run [bold]bernstein cleanup[/bold] if stale worktrees or logs are accumulating."
            )

    # Cost confirmation is skipped when the plan approval prompt follows
    # (it already shows cost and asks Y/N — no need to ask twice).
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

    Args:
        quiet: When True, wait for quiescence and print only the terminal summary.
    """
    from bernstein.cli.run_bootstrap import _wait_for_run_completion, exec_restart

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
        _show_run_summary()


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
