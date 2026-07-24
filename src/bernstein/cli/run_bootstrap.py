"""Main Click commands and execution bootstrap for Bernstein runs."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import click
import httpx

from bernstein.cli.first_run_guard import handle_first_run_exception
from bernstein.cli.helpers import (
    SDD_DIRS,
    SERVER_URL,
    adapter_cli_choice,
    auth_headers,
    console,
    find_seed_file,
    persist_server_port,
    print_banner,
    print_startup_banner,
    server_get,
)
from bernstein.cli.run_preflight import (
    _abort_if_default_branch_merge_target,
    _configure_quality_gate_bypass,
    _emit_preflight_runtime_warnings,
    _estimate_run_preview,
    _finalize_run_output,
    _make_profile_ctx,
    _quiet_bootstrap_console,
    _show_run_summary,
    validate_seed_or_exit,
)
from bernstein.core.cost import estimate_run_cost
from bernstein.core.manager_parsing import _resolve_depends_on  # pyright: ignore[reportPrivateUsage]
from bernstein.core.plan_loader import PlanLoadError, load_plan, load_plan_from_yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Plan helpers
# ---------------------------------------------------------------------------


def _build_synthetic_plan(goal: str, team: list[str] | None = None) -> tuple[Any, list[Any]]:
    """Build a synthetic TaskPlan from a goal for --plan-only or confirmation display.

    Args:
        goal: Project goal string.
        team: Optional list of role names. Defaults to ["manager"].

    Returns:
        Tuple of (TaskPlan, list[Task]).
    """
    from bernstein.core.models import Complexity, Scope, Task
    from bernstein.core.plan_approval import create_plan

    roles = team or ["manager"]
    tasks: list[Task] = [
        Task(
            id=f"planned-{i + 1}",
            title=f"[{role}] {goal[:70]}",
            description=goal,
            role=role,
            priority=i + 1,
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
        )
        for i, role in enumerate(roles)
    ]
    plan = create_plan(goal, tasks)
    return plan, tasks


def _resolve_goal_and_team(workdir: Path, goal: str | None, seed_file: str | None) -> tuple[str, list[str] | None]:
    """Resolve the effective goal and team from an inline goal or a seed.

    Shared by ``--plan-only`` and ``--dry-run`` so both preview the same plan
    from the same source and reject the same seeds. When ``goal`` is given it
    wins; otherwise the seed file is parsed (``parse_seed``), which enforces the
    exact validation a real run enforces - so a seed the run would reject (for
    example an unselectable ``cli:``) is rejected during preview too, instead of
    being previewed and then crashing at spawn time (issues #2800, #2807).

    Raises:
        SystemExit: On a missing goal/seed or a seed that fails validation.
    """
    if goal is not None:
        return goal, None

    from bernstein.core.seed import SeedError, parse_seed

    if seed_file is not None:
        seed_path = Path(seed_file)
    else:
        found = find_seed_file()
        if found is None:
            from bernstein.cli.errors import no_seed_or_goal

            no_seed_or_goal().print()
            raise SystemExit(1)
        seed_path = found

    try:
        seed = parse_seed(seed_path)
    except SeedError as exc:
        from bernstein.cli.errors import seed_parse_error

        seed_parse_error(exc).print()
        raise SystemExit(1) from exc

    team = list(seed.team) if seed.team != "auto" else None
    return seed.goal, team


def _load_plan_goal(plan_path: Path) -> str:
    """Extract the goal from a saved plan file (JSON or markdown).

    Args:
        plan_path: Path to the plan file.

    Returns:
        Goal string extracted from the plan.

    Raises:
        ValueError: If the goal cannot be extracted.
    """
    content = plan_path.read_text()

    # Try JSON first (PlanStore format)
    if plan_path.suffix == ".json":
        with suppress(json.JSONDecodeError):
            data = json.loads(content)
            if "goal" in data:
                return str(data["goal"])

    # Fall back to markdown: look for "**Goal:** ..." line
    for line in content.splitlines():
        if line.startswith("**Goal:**"):
            return line.replace("**Goal:**", "").strip()

    raise ValueError(f"Could not extract goal from plan file: {plan_path}")


def _save_plan_markdown(md: str, workdir: Path) -> Path:
    """Save rendered plan markdown to .sdd/runtime/plans/ with a timestamp name.

    Args:
        md: Markdown content to save.
        workdir: Project root directory.

    Returns:
        Path to the saved file.
    """
    plans_dir = workdir / ".sdd" / "runtime" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    plan_file = plans_dir / f"plan-{ts}.md"
    plan_file.write_text(md)
    return plan_file


def _load_dry_run_tasks(plan_file: Path | None) -> list[Any]:
    """Load tasks for a dry run from a plan file or running server.

    Args:
        plan_file: Optional plan file path.

    Returns:
        List of Task objects.

    Raises:
        SystemExit: On plan load error or server connectivity failure.
    """
    from bernstein.core.models import Task

    if plan_file is not None:
        try:
            _plan_config, tasks = load_plan(plan_file)
            return tasks
        except PlanLoadError as exc:
            console.print(f"[red]Plan load error:[/red] {exc}")
            raise SystemExit(1) from exc

    _headers: dict[str, str] = {}
    _token = os.environ.get("BERNSTEIN_AUTH_TOKEN", "")
    if _token:
        _headers["Authorization"] = f"Bearer {_token}"
    try:
        resp = httpx.get(
            "http://127.0.0.1:8052/tasks?status=open",
            headers=_headers,
            timeout=5.0,
        )
        resp.raise_for_status()
        tasks_data = resp.json()
    except httpx.ConnectError as err:
        console.print("[red]Task server not running. Start with `bernstein run` first,[/red]")
        console.print("[red]or pass a plan file: `bernstein run --dry-run plan.yaml`[/red]")
        raise SystemExit(1) from err
    except Exception as exc:
        console.print(f"[red]Failed to fetch tasks:[/red] {exc}")
        raise SystemExit(1) from exc

    return [Task.from_dict(td) for td in tasks_data]


def _confirm_run(*, goal: str | None, seed_file: str | None) -> bool:
    """Show confirmation prompt before execution. Returns True to proceed."""
    effective_goal = goal
    team: list[str] | None = None

    if effective_goal is None:
        _peek_path: Path | None = Path(seed_file) if seed_file is not None else find_seed_file()
        if _peek_path is not None:
            with suppress(Exception):
                from bernstein.core.seed import parse_seed as _parse_seed

                _seed = _parse_seed(_peek_path)
                effective_goal = _seed.goal
                team = list(_seed.team) if _seed.team != "auto" else None
                from bernstein.core.plan_approval import configure_plan_models

                configure_plan_models(_seed.role_model_policy)

    if effective_goal:
        plan_obj, plan_tasks = _build_synthetic_plan(effective_goal, team)
        from bernstein.cli.plan_display import display_plan_and_confirm

        return display_plan_and_confirm(plan_obj, plan_tasks, console=console)

    with suppress(UnicodeDecodeError, EOFError):
        if not click.confirm("\nProceed with execution?", default=True):
            console.print("[dim]Cancelled.[/dim]")
            return False
    return True


# ``--sandbox`` accepts every backend the deterministic selector knows
# about. The list mirrors :data:`bernstein.core.sandbox.selector.DEFAULT_PRECEDENCE`
# plus the cloud backends that ride entry-point registration.
SANDBOX_CHOICES: tuple[str, ...] = (
    "docker",
    "podman",
    "worktree",
    "e2b",
    "modal",
    "daytona",
    "blaxel",
    "runloop",
    "vercel",
    # Opt-in, never auto-selected: mirrors its trailing position in
    # DEFAULT_PRECEDENCE, and it is deliberately absent from
    # SANDBOX_FREE_CHOICES so only an explicit flag reaches it.
    "microvm",
)


# Subset that the selector considers "free" (no per-second cost, no API key
# needed). The CLI only widens consideration to the rest when the operator
# also passes ``--allow-paid``.
SANDBOX_FREE_CHOICES: tuple[str, ...] = ("docker", "podman", "worktree")


def _parse_budget_spec(raw: str | float | None) -> float | None:
    """Parse a budget spec like ``5usd``, ``$5``, or ``5.5`` to a float.

    Returns ``None`` when *raw* is None / blank. Negative values are
    clamped to ``0.0`` (meaning "unlimited" per
    :func:`bernstein.core.cost.cost_tracker.resolve_run_budget_usd`).
    Issue #1320: shared by ``--budget`` and ``--hard-budget``.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    s = str(raw).strip().lower()
    if not s:
        return None
    s = s.removesuffix("usd").removesuffix("$").strip()
    s = s.removeprefix("$").strip()
    try:
        v = float(s)
    except ValueError as exc:
        import click

        raise click.BadParameter(
            f"Invalid budget spec: {raw!r}. Use e.g. '5usd', '$5', or '5.0'.",
        ) from exc
    return max(0.0, v)


def _propagate_env_flags(
    *,
    profile: bool,
    workflow: str | None,
    routing: str | None,
    compliance: str | None,
    sandbox: str | None,
    container: bool,
    container_image: str | None,
    two_phase_sandbox: bool,
    quiet: bool,
    task_filter: str | None,
    auto_pr: bool,
    activity_log_path: str | None,
    audit: bool,
    max_cost_usd: float | None = None,
    hard_budget_usd: float | None = None,
    allow_paid: bool = False,
    permission_profile: str | None = None,
    max_blast_radius: float | None = None,
) -> None:
    """Set environment variables so orchestrator subprocesses inherit CLI flags."""
    _flag_map: list[tuple[bool, str]] = [
        (profile, "BERNSTEIN_PROFILE"),
        (two_phase_sandbox, "BERNSTEIN_TWO_PHASE_SANDBOX"),
        (quiet, "BERNSTEIN_QUIET"),
        (auto_pr, "BERNSTEIN_AUTO_PR"),
        (audit, "BERNSTEIN_AUDIT"),
        (allow_paid, "BERNSTEIN_SANDBOX_ALLOW_PAID"),
    ]
    for flag, key in _flag_map:
        if flag:
            os.environ[key] = "1"

    _str_map: list[tuple[str | None, str]] = [
        (workflow, "BERNSTEIN_WORKFLOW"),
        (routing, "BERNSTEIN_ROUTING"),
        (compliance, "BERNSTEIN_COMPLIANCE"),
        (container_image, "BERNSTEIN_CONTAINER_IMAGE"),
        (task_filter, "BERNSTEIN_TASK_FILTER"),
        (activity_log_path, "BERNSTEIN_ACTIVITY_LOG"),
        (permission_profile, "BERNSTEIN_PERMISSION_PROFILE"),
    ]
    for val, key in _str_map:
        if val:
            os.environ[key] = val

    # Per-run budget cap. Off-by-default: only propagated when
    # the operator passes ``--max-cost-usd`` so existing runs are unaffected.
    if max_cost_usd is not None and max_cost_usd > 0.0:
        from bernstein.core.cost_tracker import ENV_MAX_COST_USD

        os.environ[ENV_MAX_COST_USD] = f"{max_cost_usd:.6f}"

    # Hard cap kill switch (issue #1320). Independent of ``--budget`` /
    # ``--max-cost-usd``: when set, the orchestrator halts as soon as
    # cumulative spend reaches this value, no soft-warn ramp.
    if hard_budget_usd is not None and hard_budget_usd > 0.0:
        os.environ["BERNSTEIN_HARD_BUDGET_USD"] = f"{hard_budget_usd:.6f}"

    # Reversibility gate (#1322). Off-by-default: only propagated when
    # the operator passes ``--max-blast-radius`` so existing runs are
    # unaffected. The merge / deploy gate reads this env var and refuses
    # changes whose blast-radius score exceeds the ceiling.
    if max_blast_radius is not None:
        os.environ["BERNSTEIN_MAX_BLAST_RADIUS"] = f"{max_blast_radius:.4f}"

    if sandbox:
        normalized = sandbox.lower()
        # Only the kernel-isolation backends imply ``--container=1``; cloud
        # and worktree backends manage their own environment so we leave
        # the legacy flag alone for them.
        if normalized in {"docker", "podman"}:
            os.environ["BERNSTEIN_CONTAINER"] = "1"
        os.environ["BERNSTEIN_SANDBOX_RUNTIME"] = normalized
        # Surface the paid-allowed bit so downstream callers (selector,
        # orchestrator) can honour it without re-parsing argv.
        if normalized not in SANDBOX_FREE_CHOICES and not allow_paid:
            from bernstein.cli.errors import BernsteinError

            BernsteinError(
                what=f"Sandbox {normalized!r} requires --allow-paid",
                why="Paid cloud backends are gated behind an explicit opt-in to avoid surprise spend",
                fix="Re-run with --allow-paid, or pick a free backend (worktree, docker, podman)",
            ).print()
            raise SystemExit(2)
    elif container:
        os.environ["BERNSTEIN_CONTAINER"] = "1"


def _install_network_policy(
    *,
    run_profile: str | None,
    allow_network: tuple[str, ...],
) -> None:
    """Install the egress policy for this run and propagate to children.

    Default outside ``--profile airgap`` is unrestricted (back-compat).
    Default inside ``--profile airgap`` is deny-all unless the operator
    overrides with one or more ``--allow-network`` flags.

    Raises:
        NetworkPolicyConfigError: When ``--profile airgap`` is combined
            with ``--allow-network any``. The combination silently
            disables the airgap boundary so we reject it at parse time.
    """
    from bernstein.core.security.network_policy import (
        PROFILE_AIRGAP,
        PROFILE_SOVEREIGN,
        NetworkPolicy,
        NetworkPolicyConfigError,
        install_policy,
    )

    profile_norm = (run_profile or "").strip().lower() or None
    # Sovereign composes the airgap network posture: the same deny-all default
    # and runtime socket guard, plus the residency posture wired up separately
    # in ``_activate_sovereign_profile``. So a network-locked profile is either
    # airgap or sovereign.
    network_locked = profile_norm in {PROFILE_AIRGAP, PROFILE_SOVEREIGN}
    if network_locked:
        for spec in allow_network:
            if spec.strip().lower() == "any":
                raise NetworkPolicyConfigError(
                    f"--profile {profile_norm} is incompatible with --allow-network any: "
                    "explicit allow-list entries (host, host:port, or CIDR) are required, "
                    "or omit --allow-network to keep the deny-all default.",
                )
    if allow_network:
        policy = NetworkPolicy.from_specs(allow_network)
    elif network_locked:
        policy = NetworkPolicy.deny_all()
    else:
        policy = NetworkPolicy.allow_all()
    # Under sovereign, install the airgap network profile mode so every airgap
    # network behaviour (deny-all default, doctor airgap, socket guard) fires;
    # the dedicated sovereign marker distinguishes the superset. Both markers go
    # in through one call so the process never observes a half-set pair.
    is_sovereign = profile_norm == PROFILE_SOVEREIGN
    network_profile = PROFILE_AIRGAP if is_sovereign else profile_norm
    install_policy(policy, profile=network_profile, sovereign=is_sovereign)

    # Under a network-locked profile, also patch socket.socket.connect so an
    # un-declared outbound dial cannot bypass the per-adapter check.
    # Outside these profiles the guard self-disables (returns False) so the
    # call is safe to issue unconditionally.
    if network_locked:
        from bernstein.core.security.socket_guard import install_runtime_socket_guard

        install_runtime_socket_guard()


def _sovereign_config_snapshot(*, run_profile: str | None, workdir: Path) -> dict[str, Any] | None:
    """Load the sovereign config snapshot once, failing closed if unreadable.

    Returning a single snapshot that both the network-policy install and the
    attestation consume removes the window where the file could change between
    two independent reads and leave the attestation describing a posture the
    runtime never installed.

    Returns:
        The parsed snapshot under ``--profile sovereign``, else ``None``.

    Raises:
        SystemExit: When the sovereign config is missing or unreadable.
    """
    from bernstein.core.security.network_policy import PROFILE_SOVEREIGN

    if (run_profile or "").strip().lower() != PROFILE_SOVEREIGN:
        return None

    from bernstein.core.security.deployment_profile import SovereignConfigError, load_config_snapshot

    try:
        return load_config_snapshot(workdir, require=True)
    except SovereignConfigError as exc:
        from bernstein.cli.errors import BernsteinError

        BernsteinError(
            what="Sovereign profile cannot resolve its source configuration",
            why=(
                f"{exc}. The residency posture is a projection of bernstein.yaml; an unreadable "
                "config would resolve to a permissive default posture and be attested as though "
                "the operator had declared it."
            ),
            fix="Restore a readable bernstein.yaml in the project root, then re-run with --profile sovereign",
        ).print()
        raise SystemExit(2) from exc


def _install_profile_network_policy(
    *,
    run_profile: str | None,
    allow_network: tuple[str, ...],
    workdir: Path,
    config_snapshot: dict[str, Any] | None = None,
) -> None:
    """Install the egress policy, sourcing sovereign egress from config (#2518).

    Under ``--profile sovereign`` the egress allow-list comes from
    ``bernstein.yaml`` (``sovereign.allowed_egress``), not ``--allow-network``,
    so the runtime network policy and the signed posture attestation are
    computed from the same config and cannot diverge (a deny-all attestation
    can never coexist with a runtime that quietly allows a destination).
    Everything else keeps the existing ``--allow-network`` behaviour.

    Args:
        config_snapshot: Pre-loaded snapshot shared with the attestation step.
            ``None`` loads it here, failing closed if it cannot be read.

    Raises:
        click.UsageError: When ``--allow-network`` is combined with
            ``--profile sovereign``.
        SystemExit: When the sovereign config is missing or unreadable.
    """
    if (run_profile or "").strip().lower() == "sovereign":
        from bernstein.core.security.deployment_profile import sovereign_egress_allowlist

        if allow_network:
            raise click.UsageError(
                "--allow-network is not accepted under --profile sovereign. Declare egress "
                "destinations in bernstein.yaml under sovereign.allowed_egress so the runtime "
                "network policy and the signed posture attestation stay in sync."
            )
        snapshot = (
            config_snapshot
            if config_snapshot is not None
            else _sovereign_config_snapshot(run_profile=run_profile, workdir=workdir)
        )
        egress = sovereign_egress_allowlist(snapshot)
        _install_network_policy(run_profile=run_profile, allow_network=egress)
    else:
        _install_network_policy(run_profile=run_profile, allow_network=allow_network)


def _refuse_sovereign_activation(*, workdir: Path, violations: tuple[str, ...], policy: Any) -> None:
    """Anchor a signed refusal record and abort the run.

    The refusal is evidence, not a console message: the same signed drift
    record the spawn gate emits is anchored in the HMAC audit chain, so an
    auditor sees why the profile refused to activate and can re-verify it under
    ``bernstein audit verify``. Nothing is attested - a posture that violates
    the profile must never be sealed as this install's sovereign posture.

    Raises:
        SystemExit: Always.
    """
    import time as _time

    from bernstein.cli.errors import BernsteinError
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.security.deployment_profile import DriftEvaluation, record_and_sign_drift

    evaluation = DriftEvaluation(
        drifted=False,
        reason="sovereign posture violates the profile; activation refused before attestation",
        attested_hash="",
        observed_hash=policy.posture_hash(),
        diverging_keys=(),
        observed_policy=policy,
        violations=violations,
    )
    _, record_sha256 = record_and_sign_drift(
        workdir=workdir,
        evaluation=evaluation,
        timestamp=int(_time.time()),
        chain=AuditChainStore(workdir / ".sdd" / "audit"),
    )
    BernsteinError(
        what="Sovereign profile activation refused: the live posture violates the profile",
        why="; ".join(violations),
        fix=(
            "Fix the settings above, then re-run with --profile sovereign. "
            "Inspect the full report with 'bernstein doctor sovereign'. "
            f"Signed refusal record: {record_sha256}"
        ),
    ).print()
    raise SystemExit(2)


def _activate_sovereign_profile(
    *,
    run_profile: str | None,
    workdir: Path,
    config_snapshot: dict[str, Any] | None = None,
) -> None:
    """Attest the sovereign residency posture at run start (issue #2518).

    No-op unless ``--profile sovereign`` was selected. When active, resolves
    the effective policy from the workspace config snapshot, **enforces the
    profile before sealing anything**, then signs the policy with the install's
    Ed25519 sovereign identity and anchors the attestation in the HMAC audit
    chain.

    Enforcement before attestation is the point: the checks run here are the
    same ones the spawn gate applies (config projection, endpoint certification,
    and the attested-equals-enforced egress invariant against the network policy
    installed moments earlier). Warning and attesting anyway would put a signed
    claim of a sovereign posture on the chain for a deployment that does not
    have one - and a signature is exactly what stops an auditor looking further.

    Raises:
        SystemExit: When the config is unreadable or the posture violates the
            profile. Nothing is attested in either case.
    """
    from bernstein.core.security.network_policy import PROFILE_SOVEREIGN

    if (run_profile or "").strip().lower() != PROFILE_SOVEREIGN:
        return

    import time as _time

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.security.deployment_profile import (
        SOVEREIGN_PROFILE,
        build_posture_attestation,
        resolve_effective_policy,
        sovereign_posture_violations,
    )
    from bernstein.core.security.network_policy import policy_from_env

    snapshot = (
        config_snapshot
        if config_snapshot is not None
        else _sovereign_config_snapshot(run_profile=run_profile, workdir=workdir)
    )
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, snapshot)
    violations = sovereign_posture_violations(policy, workdir=workdir, runtime_policy=policy_from_env())
    if violations:
        _refuse_sovereign_activation(workdir=workdir, violations=violations, policy=policy)

    chain = AuditChainStore(workdir / ".sdd" / "audit")
    attestation = build_posture_attestation(
        workdir=workdir,
        policy=policy,
        timestamp=int(_time.time()),
        chain=chain,
    )
    console.print(f"[dim]Sovereign posture attested:[/dim] {attestation.posture_hash}")
    console.print(
        f"[dim]Egress posture:[/dim] {policy.network_egress}"
        + (f" {list(policy.egress_allowlist)}" if policy.egress_allowlist else "")
    )


def _show_dry_run_plan(
    workdir: Path,
    plan_file: Path | None,
    goal: str | None,
    seed_file: str | None,
    model_override: str | None,
    cli: str | None,
) -> None:
    """Show scheduling plan without executing.

    When a plan file is provided, loads tasks directly from it so no running
    server is required.  Otherwise falls back to fetching open tasks from a
    running task server.

    Args:
        _workdir: Project root directory.
        plan_file: Optional plan file path.
        _goal: Optional goal string.
        _seed_file: Optional seed file path.
        model_override: Optional model override.
        _cli: Optional CLI override.
    """
    _ = cli  # Part of interface: --cli is validated by its click.Choice
    from rich.table import Table

    console.print("\n[bold]Dry-run mode: no agents will be spawned.[/bold]\n")

    if plan_file is not None:
        tasks = _load_dry_run_tasks(plan_file)
    elif goal is not None or seed_file is not None or find_seed_file() is not None:
        # Synthesize the plan from the seed/goal exactly as --plan-only does, so
        # a seed preview needs no running task server and a seed the real run
        # would reject is rejected here too (issues #2800, #2807), instead of
        # querying a server that --dry-run never started.
        effective_goal, team = _resolve_goal_and_team(workdir, goal, seed_file)
        _plan_obj, tasks = _build_synthetic_plan(effective_goal, team)
    else:
        # No seed/goal/plan: fall back to previewing a running server's backlog.
        tasks = _load_dry_run_tasks(None)

    if not tasks:
        console.print("[yellow]No tasks to schedule.[/yellow]")
        return

    table = Table(title="Dry-Run Scheduling Plan", show_header=True, header_style="bold cyan")
    table.add_column("#", justify="right")
    table.add_column("Role", style="cyan")
    table.add_column("Title")
    table.add_column("Model", style="green")
    table.add_column("Effort")
    table.add_column("Priority", justify="center")

    for i, task in enumerate(tasks, 1):
        table.add_row(
            str(i),
            task.role,
            task.title[:60] + "..." if len(task.title) > 60 else task.title,
            task.model or "auto",
            task.effort or "auto",
            str(task.priority),
        )

    console.print(table)

    deps_found = False
    for task in tasks:
        if task.depends_on:
            if not deps_found:
                console.print("\n[bold]Dependency graph:[/bold]")
                deps_found = True
            dep_str = ", ".join(task.depends_on)
            console.print(f"  {task.title} -> [{dep_str}]")

    est_model = model_override or "sonnet"
    low_usd, high_usd = estimate_run_cost(len(tasks), est_model)
    console.print(f"\n  Total tasks: {len(tasks)}")
    console.print(f"  Estimated cost: ${(low_usd + high_usd) / 2:.2f} (${low_usd:.2f}-${high_usd:.2f})")

    console.print("\n[green]Dry run complete. No agents were spawned.[/green]")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _detect_project_type(root: Path) -> str:
    """Auto-detect project type by checking for common config files.

    Args:
        root: Project root directory.

    Returns:
        Detected project type string (e.g. "python", "node", "go", "generic").
    """
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        return "python"
    if (root / "package.json").exists():
        return "node"
    if (root / "go.mod").exists():
        return "go"
    if (root / "Cargo.toml").exists():
        return "rust"
    return "generic"


def _default_constraints_for(project_type: str) -> list[str]:
    """Return sensible default constraints for a detected project type.

    Args:
        project_type: One of the types returned by ``_detect_project_type``.

    Returns:
        List of constraint strings.
    """
    mapping: dict[str, list[str]] = {
        "python": ["Python 3.12+", "pytest for tests", "ruff for linting"],
        "node": ["Node.js", "TypeScript preferred", "vitest or jest for tests"],
        "go": ["Go modules", "go test for tests"],
        "rust": ["Cargo for builds", "cargo test for tests"],
    }
    return mapping.get(project_type, [])


def _generate_default_yaml(project_type: str) -> str:
    """Generate a default bernstein.yaml with project-aware defaults.

    Args:
        project_type: Detected project type.

    Returns:
        YAML content string.
    """
    lines = [
        "# Bernstein orchestration config",
        "# Uncomment and edit the goal, then run: bernstein",
        "",
        '# goal: "Describe what you want the agents to build or improve"',
        "",
        "cli: auto  # Bernstein picks the best agent per task",
        "team: auto",
        'budget: "$10"',
    ]
    constraints = _default_constraints_for(project_type)
    if constraints:
        lines.extend(("", "constraints:"))
        for c in constraints:
            lines.append(f'  - "{c}"')
    lines.append("")
    return "\n".join(lines)


def is_codespace_runtime() -> bool:
    """Return True when running inside a GitHub Codespace.

    GitHub injects ``CODESPACES=true`` into every Codespace shell. The
    devcontainer also sets ``BERNSTEIN_REMOTE_QUICKSTART=1`` so a manual
    opt-in is possible from any other remote-container surface that
    follows the same convention.
    """
    if os.environ.get("CODESPACES", "").lower() == "true":
        return True
    return os.environ.get("BERNSTEIN_REMOTE_QUICKSTART", "") == "1"


@click.command("init")
@click.option(
    "--dir",
    "target_dir",
    default=".",
    show_default=True,
    help="Directory to initialise (default: current directory).",
)
@click.option(
    "--add-badge",
    "add_badge",
    is_flag=True,
    default=False,
    help=(
        "Insert a shields.io 'powered by bernstein' badge into README.md. "
        "Use --badge-variant to pick the wording (default: signed)."
    ),
)
@click.option(
    "--badge-variant",
    "badge_variant",
    default="signed",
    show_default=True,
    type=click.Choice(["signed", "audited-by", "orchestrated-by", "crew-managed-by"]),
    help="Badge variant when --add-badge is passed.",
)
@click.option(
    "--remote",
    "remote",
    is_flag=True,
    default=False,
    help=(
        "Initialise for a remote container quickstart (e.g. GitHub Codespaces). "
        "Skips local-binary checks that would fail in a fresh container."
    ),
)
def init(
    target_dir: str,
    *,
    add_badge: bool = False,
    badge_variant: str = "signed",
    remote: bool = False,
) -> None:
    """Init workspace -- create .sdd/ structure."""
    try:
        _init_impl(
            target_dir,
            add_badge=add_badge,
            badge_variant=badge_variant,
            remote=remote,
        )
    except (click.UsageError, SystemExit):
        raise
    except BaseException as exc:
        handle_first_run_exception(exc, verbose=_is_verbose())


def _is_verbose() -> bool:
    """Return True when the active Click context flagged ``--verbose``."""
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return False
    raw_obj: object = ctx.obj
    if not isinstance(raw_obj, dict):
        return False
    obj: dict[str, object] = raw_obj  # type: ignore[assignment]
    return bool(obj.get("VERBOSE", False))


def _init_impl(
    target_dir: str,
    *,
    add_badge: bool,
    badge_variant: str,
    remote: bool = False,
) -> None:
    """Concrete init implementation; wrapped by :func:`init` for hinting."""
    print_banner()
    root = Path(target_dir).resolve()
    console.print(f"Initialising Bernstein workspace in [bold]{root}[/bold]")

    # Remote-quickstart mode: assume a fresh container without the local
    # CLI binaries (brew/pipx/uv may not be present yet). The flag is
    # auto-enabled when running inside a Codespace so users hitting the
    # devcontainer postCreate get the right defaults without remembering
    # the flag.
    if remote or is_codespace_runtime():
        remote = True
        console.print("[cyan]Remote-quickstart mode[/cyan]: skipping local-binary checks (Codespaces / devcontainer).")

    # Auto-detect project type. Skipped in remote mode because the
    # detection probes local toolchains (e.g. brew adapter check) that
    # are not expected to be installed in a fresh container.
    if not remote:
        project_type = _detect_project_type(root)
        if project_type != "generic":
            console.print(f"[cyan]Detected[/cyan] {project_type} project")
    else:
        project_type = "generic"

    for d in SDD_DIRS:
        p = root / d
        p.mkdir(parents=True, exist_ok=True)

    # Write a minimal default config
    config_path = root / ".sdd" / "config.yaml"
    if not config_path.exists():
        config_path.write_text(
            "# Bernstein workspace config\n"
            "server_port: 8052\n"
            "max_workers: 6\n"
            "default_model: sonnet\n"
            "default_effort: high\n"
        )
        console.print(f"[green]Created[/green] {config_path.relative_to(root)}")

    # Write a .gitignore for the runtime dir
    gi_path = root / ".sdd" / "runtime" / ".gitignore"
    if not gi_path.exists():
        gi_path.write_text("*.pid\n*.log\n")

    # Create bernstein.yaml in project root if not present
    yaml_path = root / "bernstein.yaml"
    if not yaml_path.exists():
        yaml_path.write_text(_generate_default_yaml(project_type))
        console.print(f"[green]Created[/green] {yaml_path.relative_to(root)}")

    # Copy bundled default templates if the project doesn't have its own
    templates_dst = root / "templates"
    if not templates_dst.exists():
        import shutil

        from bernstein import _BUNDLED_TEMPLATES_DIR  # type: ignore[reportPrivateUsage]

        if _BUNDLED_TEMPLATES_DIR.is_dir():
            shutil.copytree(_BUNDLED_TEMPLATES_DIR, templates_dst)
            console.print("[green]Created[/green] templates/ (default roles & prompts)")

    # Append .sdd/runtime/ to root .gitignore if not already present
    root_gi_path = root / ".gitignore"
    gitignore_entry = ".sdd/runtime/"
    if root_gi_path.exists():
        existing = root_gi_path.read_text()
        if gitignore_entry not in existing:
            root_gi_path.write_text(existing.rstrip("\n") + f"\n{gitignore_entry}\n")
            console.print(f"[green]Updated[/green] .gitignore (added {gitignore_entry})")
    else:
        root_gi_path.write_text(f"{gitignore_entry}\n")
        console.print(f"[green]Created[/green] .gitignore (added {gitignore_entry})")

    # Optional: inject a "powered by bernstein" badge into README.md
    if add_badge:
        from bernstein.cli.badge import get_variant, inject_badge

        readme_path = root / "README.md"
        try:
            variant = get_variant(badge_variant)
        except KeyError:
            console.print(f"[yellow]Skipped[/yellow] badge: unknown variant {badge_variant!r}")
        else:
            if not readme_path.exists():
                console.print("[yellow]Skipped[/yellow] badge: README.md not found")
            else:
                changed = inject_badge(readme_path, variant)
                if changed:
                    console.print(f"[green]Updated[/green] README.md (added '{variant.name}' badge)")
                else:
                    console.print("[dim]README.md already has a bernstein badge - skipped.[/dim]")

    # Print clear next steps
    console.print("")
    console.print("[green]Done.[/green] Next steps:")
    console.print("  1. Edit [bold]bernstein.yaml[/bold]: set a goal")
    console.print("  2. Run [bold]bernstein[/bold] to start the orchestra")
    console.print("")
    console.print(
        "  See [link=https://bernstein.readthedocs.io/en/latest/]docs[/link] "
        "or [bold]examples/quickstart/[/bold] for a working example."
    )


def _signal_orchestrator_shutdown(*, reason: str = "cli detected run completion") -> None:
    """Best-effort belt-and-braces graceful-shutdown signal to the orchestrator.

    The orchestrator has its own quiescence self-stop (see
    ``core/orchestration/orchestrator.py`` tick-quiescence handling) that
    should already have terminated the run by the time the CLI observes
    completion. This call exists purely as a backstop: if the self-stop
    ever fails to fire, the CLI still nudges the server's ``POST /shutdown``
    route (see ``core/routes/status_lifecycle.py::shutdown_server``) so the
    run does not hang forever.

    This call is idempotent and never raises: a connection error or 404
    almost always means the orchestrator already stopped itself, which is
    treated as success (not a failure of this signal).
    """
    target = f"{SERVER_URL}/shutdown"
    try:
        resp = httpx.post(
            target,
            json={"reason": reason},
            timeout=3.0,
            headers=auth_headers(),
        )
        if resp.status_code == 404:
            logger.info(
                "cli_shutdown_signal: sent target=%s result=already_stopped "
                "(404 - server likely self-stopped and torn down its routes)",
                target,
            )
            return
        resp.raise_for_status()
        logger.info(
            "cli_shutdown_signal: sent target=%s result=acknowledged response=%s",
            target,
            resp.json() if resp.content else None,
        )
    except httpx.ConnectError:
        logger.info(
            "cli_shutdown_signal: sent target=%s result=already_stopped "
            "(connection refused - orchestrator process already exited via self-stop)",
            target,
        )
    except Exception as exc:
        logger.info(
            "cli_shutdown_signal: sent target=%s result=error error=%s "
            "(non-fatal - orchestrator quiescence self-stop is the primary path)",
            target,
            exc,
        )


def _wait_for_run_completion(
    *,
    poll_interval_s: float = 2.0,
    timeout_s: float = 3600.0,
) -> dict[str, Any] | None:
    """Poll the server until the run becomes quiescent.

    Args:
        poll_interval_s: Delay between status polls.
        timeout_s: Maximum total time to wait.

    Returns:
        Final ``/status`` payload when quiescent, else the last observed payload.
    """
    deadline = time.time() + timeout_s
    last_status: dict[str, Any] | None = None
    while time.time() < deadline:
        status_payload = server_get("/status")
        health_payload = server_get("/health")
        if isinstance(status_payload, dict):
            last_status = status_payload
        if isinstance(status_payload, dict) and isinstance(health_payload, dict):
            total = int(status_payload.get("total", 0) or 0)
            open_count = int(status_payload.get("open", 0) or 0)
            claimed_count = int(status_payload.get("claimed", 0) or 0)
            agent_count = int(health_payload.get("agent_count", 0) or 0)
            if total > 0 and open_count == claimed_count == 0 and (agent_count == 0):
                logger.info(
                    "run_completion_detected: total=%d open=%d claimed=%d agent_count=%d "
                    "- sending belt-and-braces shutdown signal",
                    total,
                    open_count,
                    claimed_count,
                    agent_count,
                )
                _signal_orchestrator_shutdown(reason="cli detected run completion (quiescent)")
                return status_payload
        time.sleep(poll_interval_s)
    return last_status


#: How long the exiting CLI waits for the first spawn outcome (roughly one
#: spawner tick plus slack) before detaching without a verdict.
_FIRST_SPAWN_WAIT_S = 10.0
#: Delay between first-spawn outcome polls.
_FIRST_SPAWN_POLL_S = 0.5
#: Consecutive unreachable-server polls before giving up early.
_FIRST_SPAWN_MAX_UNREACHABLE = 3
#: Failed tasks older than this are attributed to a previous run and ignored.
_FIRST_SPAWN_FRESHNESS_S = 300.0


def _await_first_spawn_outcome(
    *,
    timeout_s: float = _FIRST_SPAWN_WAIT_S,
    poll_interval_s: float = _FIRST_SPAWN_POLL_S,
) -> tuple[str, str | None]:
    """Briefly poll the task server for the outcome of the first agent spawn.

    The CLI detaches right after bootstrap, so a spawn refusal in the
    background orchestrator would otherwise never reach the terminal and
    ``bernstein run`` would exit 0 with an empty summary (gh-2744).

    A failed task counts only when its failure reason is a spawn failure and
    it completed recently; failed tasks reloaded from a previous run are
    ignored.  Transient spawn failures are given the rest of the window to
    recover before being reported.

    Args:
        timeout_s: Maximum total time to wait for a verdict.
        poll_interval_s: Delay between polls.

    Returns:
        ``("spawned", None)`` once at least one agent is live,
        ``("refused", reason)`` when the first spawn attempt failed before
        any agent did work, or ``("unknown", None)`` when no verdict arrived
        within ``timeout_s`` (including an unreachable server).
    """
    deadline = time.time() + timeout_s
    transient_reason: str | None = None
    unreachable_polls = 0
    while True:
        health = server_get("/health")
        if not isinstance(health, dict):
            unreachable_polls += 1
            if unreachable_polls >= _FIRST_SPAWN_MAX_UNREACHABLE:
                return "unknown", None
        else:
            unreachable_polls = 0
            if int(health.get("agent_count", 0) or 0) > 0:
                return "spawned", None
            failed_page: Any = server_get("/tasks?status=failed&limit=50")
            entries = failed_page.get("tasks", []) if isinstance(failed_page, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                reason = str(entry.get("result_summary") or "")
                if not reason.startswith("Spawn failed"):
                    continue
                completed_at = float(entry.get("completed_at") or 0.0)
                if completed_at < time.time() - _FIRST_SPAWN_FRESHNESS_S:
                    continue
                if "(transient" in reason:
                    # A retry may still succeed - keep polling until deadline.
                    transient_reason = reason
                    continue
                return "refused", reason
        if time.time() >= deadline:
            break
        time.sleep(poll_interval_s)
    if transient_reason is not None:
        return "refused", transient_reason
    return "unknown", None


def exec_restart() -> None:
    """Re-exec the current process as ``bernstein run`` (full stack restart).

    On macOS/Linux, uses ``os.execv`` which replaces the current process
    image entirely - no orphan.  On Windows, ``os.execv`` does not truly
    replace the process (it spawns a child), so we use ``subprocess.Popen``
    and ``sys.exit`` instead.
    """
    import subprocess

    argv = [sys.executable, "-m", "bernstein.cli.main", "run"]
    if sys.platform == "win32":
        # Windows: execv creates a child process and the parent stays alive,
        # so we spawn explicitly and exit the current process.
        subprocess.Popen(argv, close_fds=True)
        raise SystemExit(0)
    else:
        # Unix: execv replaces the process image - clean restart.
        os.execv(sys.executable, argv)


# ---------------------------------------------------------------------------
# run  (the "one command" Seed UX)
# ---------------------------------------------------------------------------


@click.command("conduct", hidden=True)
@click.argument(
    "plan_file",
    required=False,
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--goal",
    default=None,
    help="Inline goal (skips bernstein.yaml).",
)
@click.option(
    "--seed",
    "seed_file",
    default=None,
    help="Path to a custom seed YAML file (default: bernstein.yaml).",
)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Port for the task server.",
)
@click.option(
    "--cells",
    default=1,
    show_default=True,
    help="Number of parallel orchestration cells (1 = single-cell, >1 = MultiCellOrchestrator).",
)
@click.option(
    "--remote",
    is_flag=True,
    default=False,
    help="Bind server to 0.0.0.0 for remote/cluster access (default: 127.0.0.1).",
)
@click.option(
    "--cli",
    default=None,
    type=adapter_cli_choice(),
    help="Force specific CLI agent (overrides auto-detection and config file).",
)
@click.option(
    "--model",
    default=None,
    help="Force specific model (e.g. opus, sonnet, o4-mini; overrides config file).",
)
@click.option(
    "--workflow",
    default=None,
    type=click.Choice(["governed"], case_sensitive=False),
    help="Activate a governed workflow mode (deterministic phase-based execution).",
)
@click.option(
    "--routing",
    default=None,
    type=click.Choice(["static", "bandit", "bandit-shadow"], case_sensitive=False),
    help=(
        "Model routing strategy: 'static' = fixed cascade heuristics (default), "
        "'bandit' = contextual LinUCB bandit that learns cost-quality tradeoffs, "
        "'bandit-shadow' = log bandit decisions without changing live routing."
    ),
)
@click.option(
    "--compliance",
    default=None,
    type=click.Choice(["development", "standard", "regulated"], case_sensitive=False),
    help=(
        "Compliance preset: 'development' = audit + WAL + AI labels, "
        "'standard' = + HMAC chain + governed workflow + approval gates, "
        "'regulated' = + signed WAL + data residency + SBOM + evidence bundle."
    ),
)
@click.option(
    "--container/--no-container",
    default=False,
    help="Run agents inside containers for kernel-level isolation (requires Docker or Podman).",
)
@click.option(
    "--container-image",
    default=None,
    help="Container image for agent execution (default: bernstein-agent:latest). Requires --container.",
)
@click.option(
    "--sandbox",
    default=None,
    type=click.Choice(list(SANDBOX_CHOICES), case_sensitive=False),
    help=(
        "Sandbox backend for agent isolation. Free: worktree, docker, podman. "
        "Paid (require --allow-paid): e2b, modal, daytona, blaxel, runloop, vercel. "
        "Overrides the selector's deterministic precedence; pass nothing to let "
        "the selector pick the cheapest backend that satisfies the manifest."
    ),
)
@click.option(
    "--allow-paid",
    "allow_paid",
    is_flag=True,
    default=False,
    help=(
        "Allow the sandbox selector to consider paid cloud backends "
        "(e2b, modal, daytona, blaxel, runloop, vercel). Off by default so "
        "selector falls back to free backends only. Required when --sandbox "
        "names a paid backend explicitly."
    ),
)
@click.option(
    "--two-phase-sandbox/--no-two-phase-sandbox",
    default=False,
    help=(
        "Codex-style two-phase sandboxed execution: "
        "Phase 1 installs dependencies with network access, "
        "Phase 2 runs the agent with the network fully disabled. "
        "Requires --container."
    ),
)
@click.option(
    "--worker",
    "worker_role",
    default=None,
    help=(
        "Skip manager decomposition and spawn a single agent with this role "
        "(e.g. backend, qa, frontend) directly against the seed goal."
    ),
)
@click.option(
    "--plan-only",
    is_flag=True,
    default=False,
    help="Generate and display the execution plan without running any agents.",
)
@click.option(
    "--from-plan",
    "from_plan",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Load a saved plan file and execute it (skips interactive planning).",
)
@click.option(
    "--auto-approve",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt before execution.",
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress startup/TUI output and print only the final summary.",
)
@click.option(
    "--skip-gate",
    "skip_gate",
    multiple=True,
    help="Bypass a named quality gate for this run (requires quality_gates.allow_bypass: true).",
)
@click.option(
    "--skip-gate-reason",
    default=None,
    help="Operator-visible reason recorded for quality-gate bypasses.",
)
@click.option(
    "--audit",
    is_flag=True,
    default=False,
    help=(
        "Enable SOC 2 audit mode: append-only HMAC-chained audit log for every "
        "task lifecycle event, with Merkle tree seal on shutdown."
    ),
)
@click.option(
    "--ab-test",
    is_flag=True,
    default=False,
    help="A/B testing mode: spawn two agents with different models for each task.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show scheduling plan without executing: which agent/model/tier each task would be assigned to.",
)
@click.option(
    "--idle",
    is_flag=True,
    default=False,
    help=(
        "GUI-dev mode: force every adapter to ``mock`` and have each spawned "
        "agent sleep for $BERNSTEIN_MOCK_IDLE_MIN_S..MAX_S seconds (defaults: "
        "min=15, max=120) instead of calling an LLM. Zero token spend - used "
        "to populate the web GUI with live state. --idle forces the mock "
        "backend internally (it exports BERNSTEIN_ADAPTER=mock), so no "
        "bernstein.yaml edit is needed. Mutually exclusive with --dry-run."
    ),
)
@click.option(
    "--cprofile",
    "cprofile",
    is_flag=True,
    default=False,
    help=(
        "Profile orchestrator execution with cProfile. Writes .prof binary and .txt report to .sdd/runtime/profiles/."
    ),
)
@click.option(
    "--profile",
    "run_profile",
    default=None,
    type=click.Choice(["airgap", "sovereign"], case_sensitive=False),
    help=(
        "Run profile. 'airgap' = no network egress by default, MCP catalog disabled, "
        "memo store pinned to .sdd/runtime/memo/. 'sovereign' = airgap network posture "
        "plus a signed, chain-anchored residency posture (local storage, offline catalog, "
        "strict EU residency, certified endpoints) that refuses spawns on drift. Combine "
        "with --allow-network to open specific destinations."
    ),
)
@click.option(
    "--allow-network",
    "allow_network",
    multiple=True,
    metavar="HOST|CIDR|HOST:PORT|none|any",
    help=(
        "Allow-list outbound network destinations. May be repeated. "
        "Use 'none' for explicit deny-all (the --profile airgap default), "
        "'any' to opt out of the gate (legacy default)."
    ),
)
@click.option(
    "--permission-profile",
    "permission_profile",
    default=None,
    type=click.Choice(
        ["read-only", "builder", "reviewer", "custom"],
        case_sensitive=False,
    ),
    help=(
        "Per-tool permission profile (roadmap #1318). 'read-only' = review/explore "
        "agents only; 'builder' = write + shell on an allowlist; 'reviewer' = "
        "read + diff only; 'custom' = honour [permissions.custom] in "
        "bernstein.yaml/bernstein.toml. Off by default to preserve existing behaviour."
    ),
)
@click.option(
    "--task",
    "-t",
    "task_filter",
    default=None,
    metavar="PATTERN",
    help="Run only backlog tasks matching PATTERN (e.g. 'gh-62' or 'mutant-fish').",
)
@click.option(
    "--auto-pr",
    is_flag=True,
    default=False,
    help="Automatically create a GitHub PR when all tasks complete.",
)
@click.option(
    "--activity-log",
    "activity_log_path",
    is_flag=False,
    flag_value=".sdd/logs/activity.log",
    default=None,
    help="Write activity to log file (default: .sdd/logs/activity.log).",
)
@click.option(
    "--max-cost-usd",
    "max_cost_usd",
    type=float,
    default=None,
    help=(
        "Cost autopilot: hard cap on total run spend in USD. Aggregated "
        "across all agents; orchestrator stops spawning when reached. "
        "Off-by-default (overrides bernstein.yaml ``budget`` and run_config.json)."
    ),
)
@click.option(
    "--budget",
    "budget_spec",
    type=str,
    default=None,
    metavar="SPEC",
    help=(
        "Soft cap on total run spend (issue #1320). Accepts ``5usd``, "
        "``$5``, or ``5.0``. Warns + reroutes to a cheaper model at 80%, "
        "halts new work at 100%. Alias of ``--max-cost-usd`` with friendlier "
        "parsing; both flags set the same env var."
    ),
)
@click.option(
    "--hard-budget",
    "hard_budget_spec",
    type=str,
    default=None,
    metavar="SPEC",
    help=(
        "Hard cap kill switch (issue #1320). Accepts ``10usd``, ``$10``, "
        "or ``10.0``. Independent of ``--budget``: once tripped no new "
        "agent is spawned, no retry."
    ),
)
@click.option(
    "--budget-cap",
    "budget_cap",
    type=float,
    default=None,
    help=(
        "Preflight budget cap in USD. Aborts spawn before the orchestrator "
        "starts when the calibrated p90 of the cost band exceeds the cap. "
        "Distinct from --max-cost-usd / --hard-budget, which are enforced at runtime."
    ),
)
@click.option(
    "--retry-budget",
    "retry_budget_spec",
    type=str,
    default=None,
    metavar="SPEC",
    help=(
        "Criterion-aware retry budget (issue #1352). Accepts "
        "``'3 retries, degrade: coverage>tests>style'``. Each retry "
        "dials down the next criterion rather than burning identical "
        "rerun budget. Validated eagerly; the parsed spec is exported "
        "to ``BERNSTEIN_RETRY_BUDGET_SPEC`` for the orchestrator."
    ),
)
@click.option(
    "--criterion-profile",
    "criterion_profile",
    type=str,
    default=None,
    metavar="PRESET",
    help=(
        "Per-task criterion profile applied to every task in this run (issue "
        "#1346).  A named preset such as 'safety-first', 'speed-first', "
        "'balanced', or 'cost-first'.  Stamps the chosen preset onto each "
        "task's metadata so the router biases model selection accordingly. "
        "Individual tasks may still override by setting "
        "metadata['criterion_profile'] explicitly."
    ),
)
@click.option(
    "--max-blast-radius",
    "max_blast_radius",
    type=click.FloatRange(0.0, 1.0),
    default=None,
    help=(
        "Reversibility gate (#1322): refuse merges whose blast-radius score "
        "exceeds the supplied ceiling [0, 1]. Hard one-way changes (DROP/"
        "DELETE SQL, rm -rf, schema migrations, secrets writes) always "
        "score 1.0. Off by default; existing runs are unaffected."
    ),
)
@click.option(
    "--attach",
    "attach",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    multiple=True,
    default=(),
    help=(
        "Attach an image / diagram to the run (issue #1797). May be repeated "
        "for multiple files. The orchestrator builds a MultiModalContext at "
        "spawn time, records a multimodal.attach event in the audit chain, "
        "and refuses adapters that do not advertise multimodal capability. "
        "Capable adapters: claude, gemini."
    ),
)
@click.option(
    "--refresh-cache",
    "refresh_cache",
    is_flag=True,
    default=False,
    help=(
        "Cache policy bypass (issue #2551): ignore every policy cache hit for "
        "this run, then repopulate the cache with the fresh outputs. Exports "
        "BERNSTEIN_REFRESH_CACHE=1 for the orchestrator; tasks with no declared "
        "cache policy are unaffected."
    ),
)
@click.option(
    "--fresh",
    "force_fresh",
    is_flag=True,
    default=False,
    help="Ignore any saved session and start from scratch.",
)
def run(
    plan_file: Path | None,
    goal: str | None,
    seed_file: str | None,
    port: int,
    cells: int,
    remote: bool,
    cli: str | None,
    model: str | None,
    workflow: str | None,
    routing: str | None,
    compliance: str | None,
    container: bool,
    container_image: str | None,
    two_phase_sandbox: bool,
    worker_role: str | None = None,
    plan_only: bool = False,
    from_plan: Path | None = None,
    auto_approve: bool = False,
    quiet: bool = False,
    skip_gate: tuple[str, ...] = (),
    skip_gate_reason: str | None = None,
    audit: bool = False,
    sandbox: str | None = None,
    allow_paid: bool = False,
    ab_test: bool = False,
    dry_run: bool = False,
    idle: bool = False,
    cprofile: bool = False,
    run_profile: str | None = None,
    allow_network: tuple[str, ...] = (),
    permission_profile: str | None = None,
    task_filter: str | None = None,
    auto_pr: bool = False,
    activity_log_path: str | None = None,
    max_cost_usd: float | None = None,
    budget_spec: str | None = None,
    hard_budget_spec: str | None = None,
    budget_cap: float | None = None,
    retry_budget_spec: str | None = None,
    criterion_profile: str | None = None,
    max_blast_radius: float | None = None,
    attach: tuple[Path, ...] = (),
    refresh_cache: bool = False,
    force_fresh: bool = False,
) -> None:
    """Parse seed, init workspace, start server, launch agents.

    Top-level wrapper: routes any uncaught exception through the
    first-run categorisation guard so the operator sees a structured
    hint panel and a sysexits.h exit code instead of a raw traceback.
    """
    try:
        _run_impl(
            plan_file=plan_file,
            goal=goal,
            seed_file=seed_file,
            port=port,
            cells=cells,
            remote=remote,
            cli=cli,
            model=model,
            workflow=workflow,
            routing=routing,
            compliance=compliance,
            container=container,
            container_image=container_image,
            two_phase_sandbox=two_phase_sandbox,
            worker_role=worker_role,
            plan_only=plan_only,
            from_plan=from_plan,
            auto_approve=auto_approve,
            quiet=quiet,
            skip_gate=skip_gate,
            skip_gate_reason=skip_gate_reason,
            audit=audit,
            sandbox=sandbox,
            allow_paid=allow_paid,
            ab_test=ab_test,
            dry_run=dry_run,
            idle=idle,
            cprofile=cprofile,
            run_profile=run_profile,
            allow_network=allow_network,
            permission_profile=permission_profile,
            task_filter=task_filter,
            auto_pr=auto_pr,
            activity_log_path=activity_log_path,
            max_cost_usd=max_cost_usd,
            budget_spec=budget_spec,
            hard_budget_spec=hard_budget_spec,
            budget_cap=budget_cap,
            retry_budget_spec=retry_budget_spec,
            criterion_profile=criterion_profile,
            max_blast_radius=max_blast_radius,
            attach=attach,
            refresh_cache=refresh_cache,
            force_fresh=force_fresh,
        )
    except (click.UsageError, SystemExit):
        raise
    except BaseException as exc:
        handle_first_run_exception(exc, verbose=_is_verbose())


def _run_impl(
    *,
    plan_file: Path | None,
    goal: str | None,
    seed_file: str | None,
    port: int,
    cells: int,
    remote: bool,
    cli: str | None,
    model: str | None,
    workflow: str | None,
    routing: str | None,
    compliance: str | None,
    container: bool,
    container_image: str | None,
    two_phase_sandbox: bool,
    worker_role: str | None = None,
    plan_only: bool,
    from_plan: Path | None,
    auto_approve: bool,
    quiet: bool,
    skip_gate: tuple[str, ...],
    skip_gate_reason: str | None,
    audit: bool,
    sandbox: str | None,
    allow_paid: bool,
    ab_test: bool,
    dry_run: bool,
    idle: bool,
    cprofile: bool,
    run_profile: str | None,
    allow_network: tuple[str, ...],
    permission_profile: str | None,
    task_filter: str | None,
    auto_pr: bool,
    activity_log_path: str | None,
    max_cost_usd: float | None,
    budget_spec: str | None,
    hard_budget_spec: str | None,
    budget_cap: float | None,
    retry_budget_spec: str | None,
    criterion_profile: str | None,
    max_blast_radius: float | None,
    attach: tuple[Path, ...] = (),
    refresh_cache: bool = False,
    force_fresh: bool = False,
) -> None:
    """Concrete ``run`` implementation; wrapped by :func:`run` for hinting.

    \b
      bernstein run plan.yaml                  # loadable YAML plan (stages + steps)
      bernstein run                            # reads bernstein.yaml
      bernstein run --goal "Build X"           # inline goal
      bernstein run --seed custom.yaml         # custom seed file
      bernstein run --plan-only                # show plan without executing
      bernstein run --from-plan plan.md        # execute a saved plan
      bernstein run --auto-approve             # skip confirmation prompt
      bernstein run --cells 3                  # 3 parallel cells (multi-cell mode)
      bernstein run --remote                   # bind to 0.0.0.0 for cluster access
      bernstein run --cli claude               # force Claude Code agent
      bernstein run --model opus               # force Opus model
      bernstein run --workflow governed        # governed workflow mode
      bernstein run --routing bandit           # contextual bandit routing (learns over time)
      bernstein run --routing bandit-shadow    # log bandit decisions without changing live routing
      bernstein run --compliance standard      # compliance mode (development/standard/regulated)
      bernstein run --container                # run agents in containers
      bernstein run --sandbox docker           # run agents in Docker sandbox
      bernstein run --container --two-phase-sandbox  # two-phase sandboxed execution
      bernstein run --audit                    # SOC 2 audit mode (HMAC-chained log + Merkle seal)
      bernstein run --max-cost-usd 1.50        # hard cap total run spend at $1.50
      bernstein run plan.yaml --budget 5usd --hard-budget 10usd  # soft + hard caps (#1320)
      bernstein run --budget-cap 5.00          # abort spawn if preflight p90 > $5
    """
    # Opt-in operator observability (spec 2026-05-17).  Defaults to off.
    # Prints the one-time notice and emits first_run_* events around the
    # body.  Fail-closed: any internal failure is swallowed.
    with suppress(Exception):
        from bernstein.core.telemetry.wire import maybe_print_first_run_notice

        maybe_print_first_run_notice()

    _telemetry_first_run_timer: Any = None
    try:
        from bernstein.core.telemetry.wire import FirstRunTimer

        _telemetry_first_run_timer = FirstRunTimer()
        _telemetry_first_run_timer.__enter__()
    except Exception:
        _telemetry_first_run_timer = None

    # Issue #1346: validate the run-level criterion profile early so a
    # typo aborts the run before agents spawn.  The resolved name is
    # propagated to spawning code via an env var so child processes
    # pick it up without threading another argument through the
    # orchestrator bootstrap surface.
    if criterion_profile is not None:
        from bernstein.core.routing.criterion_profile import (
            CriterionProfileError,
            resolve,
        )

        try:
            resolve(criterion_profile)
        except CriterionProfileError as exc:
            raise click.UsageError(f"--criterion-profile {criterion_profile!r}: {exc}") from None
        os.environ["BERNSTEIN_RUN_CRITERION_PROFILE"] = criterion_profile

    # Issue #1797: capability-gate ``--attach`` BEFORE any process is
    # launched. When the operator selected an adapter that does not
    # advertise multimodal capability, surface a structured error that
    # names capable adapters instead of spawning the run and failing
    # mid-flight.
    if attach:
        from bernstein.core.agents.multimodal_attestation import (
            CapabilityRefusal,
            refuse_when_incapable,
        )

        # ``cli`` may be ``None`` (auto-detect) or "auto". When the
        # operator has not pinned an adapter, only refuse if every
        # candidate is incapable; with "claude" / "gemini" auto-detect
        # is allowed.
        explicit_adapter = (cli or "").strip().lower()
        if explicit_adapter and explicit_adapter not in {"", "auto"}:
            try:
                refuse_when_incapable(
                    adapter_name=explicit_adapter,
                    attachments=[str(p) for p in attach],
                )
            except CapabilityRefusal as exc:
                raise click.UsageError(f"--attach requires a multimodal-capable adapter. {exc!s}") from None
        # Stash the attachments for downstream consumers via env var so
        # the orchestrator subprocess can pick them up without
        # additional argument threading.
        os.environ["BERNSTEIN_RUN_ATTACHMENTS"] = os.pathsep.join(str(p) for p in attach)

    # Issue #1320: ``--budget`` is the friendlier alias of ``--max-cost-usd``
    # and shares the same env var. When both are set, the operator's
    # explicit ``--max-cost-usd`` wins for backward compat.
    budget_value = _parse_budget_spec(budget_spec)
    if budget_value is not None and max_cost_usd is None:
        max_cost_usd = budget_value
    hard_budget_usd = _parse_budget_spec(hard_budget_spec)
    # Issue #1352: validate the criterion-aware retry budget eagerly so
    # the operator sees parse errors before any process is spawned.  The
    # parsed budget itself is reconstructed by the orchestrator from the
    # env var; we only need to confirm the spec is well-formed here.
    if retry_budget_spec is not None:
        from bernstein.core.cost.retry_budget import (
            RetryBudgetError,
            parse_retry_budget_spec,
        )

        try:
            parse_retry_budget_spec(retry_budget_spec)
        except (RetryBudgetError, ValueError) as exc:
            raise click.UsageError(f"invalid --retry-budget value: {exc}") from exc
        os.environ["BERNSTEIN_RETRY_BUDGET_SPEC"] = retry_budget_spec
    # Issue #2551: --refresh-cache bypasses policy cache hits for this run and
    # repopulates. Exported so the orchestrator's cache boundary reads it via
    # ``bernstein.core.persistence.cache_policy.refresh_requested``. Tasks with
    # no declared policy are unaffected.
    if refresh_cache:
        os.environ["BERNSTEIN_REFRESH_CACHE"] = "1"
    # Print the startup banner unless the parent ``cli()`` group already
    # rendered the premium splash for this invocation. Regressed by commit
    # 1e5c13013 ("fix: ... double banner ..."), which mistakenly removed the
    # call assuming cli() always printed it -- cli() actually early-returns
    # for subcommand invocations, so `bernstein run` lost the banner entirely.
    ctx = click.get_current_context(silent=True)
    if ctx is None or not (ctx.obj and ctx.obj.get("_BANNER_PRINTED")):
        print_startup_banner()

    # Set process title so orchestrator is visible in Activity Monitor / ps
    with suppress(ImportError):
        import setproctitle

        setproctitle.setproctitle("bernstein: orchestrator")

    from bernstein.core.bootstrap import (  # pyright: ignore[reportUnknownVariableType]
        bootstrap_from_goal,
        bootstrap_from_seed,
    )
    from bernstein.core.seed import SeedError

    _propagate_env_flags(
        profile=cprofile,
        workflow=workflow,
        routing=routing,
        compliance=compliance,
        sandbox=sandbox,
        container=container,
        container_image=container_image,
        two_phase_sandbox=two_phase_sandbox,
        quiet=quiet,
        task_filter=task_filter,
        auto_pr=auto_pr,
        activity_log_path=activity_log_path,
        audit=audit,
        max_cost_usd=max_cost_usd,
        hard_budget_usd=hard_budget_usd,
        allow_paid=allow_paid,
        permission_profile=permission_profile,
        max_blast_radius=max_blast_radius,
    )

    # One config read feeds both the enforced network policy and the attested
    # posture, so the two cannot describe different versions of the file.
    _run_workdir = Path.cwd()
    _sovereign_snapshot = _sovereign_config_snapshot(run_profile=run_profile, workdir=_run_workdir)
    _install_profile_network_policy(
        run_profile=run_profile,
        allow_network=allow_network,
        workdir=_run_workdir,
        config_snapshot=_sovereign_snapshot,
    )
    _activate_sovereign_profile(
        run_profile=run_profile,
        workdir=_run_workdir,
        config_snapshot=_sovereign_snapshot,
    )

    _configure_quality_gate_bypass(
        goal=goal,
        seed_file=seed_file,
        skip_gate=skip_gate,
        skip_gate_reason=skip_gate_reason,
    )

    # --idle: GUI-development mode - force mock adapter + idle behavior on every spawn.
    if idle:
        if dry_run:
            raise click.UsageError("--idle and --dry-run are mutually exclusive.")
        os.environ["BERNSTEIN_MOCK_IDLE"] = "1"
        # Force mock adapter regardless of seed/config. ``cli`` is the
        # already-bound override forwarded into bootstrap; if not set, we
        # inject "mock" so the seed's adapter pick is overridden.
        if cli is None:
            cli = "mock"
        # Export the resolved adapter into the env so the orchestrator
        # subprocess (spawned via Popen later in bootstrap) honours the mock
        # backend.  Without this, the orchestrator's argparse default would
        # fall back to ``--adapter claude`` and quietly burn real tokens.
        os.environ["BERNSTEIN_ADAPTER"] = cli
        click.echo(
            "Bernstein --idle mode: every agent will be spawned via the mock adapter and sleep "
            "BERNSTEIN_MOCK_IDLE_MIN_S..MAX_S seconds (default 15-120). Zero LLM spend."
        )

    # --dry-run: show scheduling plan without executing
    if dry_run:
        _show_dry_run_plan(
            workdir=Path.cwd(),
            plan_file=plan_file,
            goal=goal,
            seed_file=seed_file,
            model_override=model,
            cli=cli,
        )
        return

    workdir = Path.cwd()
    if not plan_only:
        # Fail fast when agent merges would target the repository default
        # branch (gh-2756): without this every agent does its work and the
        # spawner merge guard then silently discards it at reap time.
        # Applies only to modes that merge agent work back -- --dry-run
        # returned above and --plan-only skips this block.
        _abort_if_default_branch_merge_target(workdir)
        # Validate the seed before estimating cost (issue #2785): a seed the
        # run would reject must not first print an estimate "at sonnet
        # pricing", and the estimate must reflect the validated seed's
        # effective default model rather than the sonnet fallback. Only seed
        # mode is validated here; inline-goal, --plan-file and --from-plan
        # runs carry no seed to validate at this point.
        validated_seed = None
        if goal is None and plan_file is None and from_plan is None:
            validated_seed = validate_seed_or_exit(seed_file)
        estimate = _estimate_run_preview(
            workdir=workdir,
            plan_file=plan_file,
            goal=goal,
            seed_file=seed_file,
            model_override=model,
            seed=validated_seed,
        )
        _emit_preflight_runtime_warnings(
            workdir=workdir,
            estimate=estimate,
            auto_approve=auto_approve,
            quiet=quiet,
            plan_approval_follows=not auto_approve,
            budget_cap=budget_cap,
        )

    # --plan_file: loadable YAML plan (stages + steps)
    if plan_file is not None:
        try:
            tasks = load_plan_from_yaml(plan_file)
            _resolve_depends_on(tasks)
            # Create a synthetic goal from the plan name
            try:
                import yaml as _yaml

                plan_data = _yaml.safe_load(plan_file.read_text())
                goal = plan_data.get("name", str(plan_file))
            except Exception:
                goal = str(plan_file)

            console.print(f"[dim]Loaded plan from:[/dim] {plan_file}")
            console.print(f"[dim]Plan name:[/dim] {goal}")
            loaded_goal = goal or str(plan_file)

            if worker_role:
                # Fail loudly instead of silently running full manager
                # decomposition, which is exactly what the flag exists
                # to bypass.
                raise click.UsageError("--worker requires a seed file; it is not supported with --plan-file.")

            with _make_profile_ctx(cprofile, workdir), _quiet_bootstrap_console(quiet):
                bootstrap_from_goal(
                    goal=loaded_goal,
                    workdir=workdir,
                    port=port,
                    cells=cells,
                    cli=cli or "auto",
                    model=model,
                    tasks=tasks,
                    ab_test=ab_test,
                )
                persist_server_port(port, workdir)

            _finalize_run_output(quiet=quiet)
            return
        except Exception as exc:
            console.print(f"[red]Failed to load plan file:[/red] {exc}")
            raise SystemExit(1) from exc

    # --from-plan: load goal from saved plan file, override inline goal
    elif from_plan is not None:
        try:
            goal = _load_plan_goal(from_plan)
            console.print(f"[dim]Loaded plan from:[/dim] {from_plan}")
            console.print(f"[dim]Goal:[/dim] {goal[:100]}")
        except (ValueError, OSError) as exc:
            console.print(f"[red]Failed to load plan:[/red] {exc}")
            raise SystemExit(1) from exc

    # --plan-only: build a synthetic plan, render to markdown, save, and exit
    if plan_only:
        from bernstein.core.plan_builder import PlanBuilder

        effective_goal, team = _resolve_goal_and_team(workdir, goal, seed_file)
        plan_obj, tasks = _build_synthetic_plan(effective_goal, team)
        builder = PlanBuilder(plan_obj, tasks)
        md = builder.render_to_markdown()

        # Render to terminal
        from rich.markdown import Markdown

        console.print(Markdown(md))

        # Save to file
        plan_file = _save_plan_markdown(md, workdir)
        console.print(f"\n[dim]Plan saved to:[/dim] {plan_file}")
        console.print(f"[dim]Execute with:[/dim] bernstein run --from-plan {plan_file}")
        return

    # Confirmation prompt before execution (skip with --auto-approve)
    if not auto_approve and not _confirm_run(goal=goal, seed_file=seed_file):
        return

    if goal is not None:
        # Inline goal mode -- no YAML needed
        if worker_role:
            # Fail loudly instead of silently running full manager
            # decomposition, which is exactly what the flag exists to
            # bypass.
            raise click.UsageError("--worker requires a seed file; it is not supported with an inline goal.")
        try:
            with _make_profile_ctx(cprofile, workdir), _quiet_bootstrap_console(quiet):
                bootstrap_from_goal(
                    goal=goal,
                    workdir=workdir,
                    port=port,
                    cells=cells,
                    cli=cli or "auto",  # Default to "auto" if not specified
                    model=model,
                    force_fresh=force_fresh,
                )
                persist_server_port(port, workdir)
        except RuntimeError as exc:
            from bernstein.cli.errors import bootstrap_failed

            bootstrap_failed(exc).print()
            raise SystemExit(1) from exc
        _finalize_run_output(quiet=quiet)
        return

    # Seed file mode
    if seed_file is not None:
        path = Path(seed_file)
    else:
        found = find_seed_file()
        if found is not None:
            path = found
        else:
            from bernstein.cli.errors import no_seed_or_goal

            no_seed_or_goal().print()
            raise SystemExit(1)

    if not quiet:
        console.print(f"[dim]Using seed file:[/dim] {path}")
    try:
        # CLI --cells overrides seed file value when explicitly set (cells > 1)
        cli_cells: int | None = cells if cells > 1 else None
        with _make_profile_ctx(cprofile, workdir), _quiet_bootstrap_console(quiet):
            bootstrap_from_seed(
                seed_path=path,
                workdir=workdir,
                port=port,
                cells=cli_cells,
                remote=remote,
                cli=cli,
                model=model,
                worker_role=worker_role,
                force_fresh=force_fresh,
            )
            persist_server_port(port, workdir)
    except SeedError as exc:
        from bernstein.cli.errors import seed_parse_error

        seed_parse_error(exc).print()
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        from bernstein.cli.errors import bootstrap_failed

        bootstrap_failed(exc).print()
        raise SystemExit(1) from exc

    _finalize_run_output(quiet=quiet)

    # Close the first-run timer (spec 2026-05-17).  Fail-closed.
    if _telemetry_first_run_timer is not None:
        import contextlib as _contextlib

        with _contextlib.suppress(Exception):
            _telemetry_first_run_timer.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# start  (legacy, kept for backward compat)
# ---------------------------------------------------------------------------


@click.command("start")
@click.argument("goal", required=False, default=None)
@click.option(
    "--seed-file",
    default="bernstein.yaml",
    show_default=True,
    help="YAML seed file to read goal/tasks from (used when GOAL is not given).",
)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Port for the task server.",
)
def start(goal: str | None, seed_file: str, port: int) -> None:
    """Start server and spawn manager (legacy alias of 'run')."""
    try:
        _start_impl(goal, seed_file, port)
    except (click.UsageError, SystemExit):
        raise
    except BaseException as exc:
        handle_first_run_exception(exc, verbose=_is_verbose())


def _start_impl(goal: str | None, seed_file: str, port: int) -> None:
    """Concrete ``start`` implementation; wrapped by :func:`start` for hinting."""
    print_banner()

    with suppress(ImportError):
        import setproctitle

        setproctitle.setproctitle("bernstein: orchestrator")

    from bernstein.core.bootstrap import (  # pyright: ignore[reportUnknownVariableType]
        bootstrap_from_goal,
        bootstrap_from_seed,
    )
    from bernstein.core.seed import SeedError

    workdir = Path.cwd()

    if goal:
        try:
            bootstrap_from_goal(goal=goal, workdir=workdir, port=port)
        except RuntimeError as exc:
            from bernstein.cli.errors import bootstrap_failed

            bootstrap_failed(exc).print()
            raise SystemExit(1) from exc
    else:
        path = Path(seed_file)
        if not path.exists():
            from bernstein.cli.errors import no_seed_file

            no_seed_file(seed_file).print()
            raise SystemExit(1)
        try:
            bootstrap_from_seed(seed_path=path, workdir=workdir, port=port)
        except SeedError as exc:
            from bernstein.cli.errors import seed_parse_error

            seed_parse_error(exc).print()
            raise SystemExit(1) from exc
        except RuntimeError as exc:
            from bernstein.cli.errors import bootstrap_failed

            bootstrap_failed(exc).print()
            raise SystemExit(1) from exc
    _show_run_summary()


@click.command("serve")
@click.option(
    "--host",
    "bind_host",
    default=None,
    help=(
        "Interface to bind. Defaults to $BERNSTEIN_BIND_HOST, else 127.0.0.1. "
        "Use 0.0.0.0 to expose a central/coordinator node to other cluster hosts."
    ),
)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    type=int,
    help="Port for the task server.",
)
@click.option(
    "--log-level",
    "log_level",
    default="info",
    show_default=True,
    help="Uvicorn log level.",
)
def serve(bind_host: str | None, port: int, log_level: str) -> None:
    """Run the task server in the foreground until stopped.

    Unlike ``bernstein run`` / ``bernstein start`` - which detach the task
    server as a background process and return - ``serve`` runs the server
    in-process and blocks until it receives SIGINT/SIGTERM. This keeps the
    process alive as PID 1 inside a container, so the published image can host a
    long-lived central/coordinator node whose ``/health`` endpoint stays
    reachable for the lifetime of the container.

    Set ``BERNSTEIN_BIND_HOST=0.0.0.0`` (or pass ``--host 0.0.0.0``) and
    ``BERNSTEIN_CLUSTER_ENABLED=1`` before start to bind all interfaces and
    expose cluster endpoints to other nodes.
    """
    import uvicorn

    host = bind_host or os.environ.get("BERNSTEIN_BIND_HOST", "127.0.0.1")
    # Keep the cluster config's advertised bind host aligned with the socket we
    # actually bind, and pin single-worker mode - the TaskStore is
    # single-process and multi-worker mode corrupts JSONL / double-claims tasks.
    os.environ["BERNSTEIN_BIND_HOST"] = host
    os.environ.setdefault("BERNSTEIN_WORKERS", "1")

    console.print(f"[green]Task server (foreground):[/green] http://{host}:{port}/  (Ctrl-C or SIGTERM to stop)")
    # In-process, blocking run against the same ASGI app the detached path
    # launches (server_launch._start_server). No start_new_session detach here:
    # the CLI process stays in the foreground so a container's PID 1 lives.
    uvicorn.run("bernstein.core.server:app", host=host, port=port, log_level=log_level)
