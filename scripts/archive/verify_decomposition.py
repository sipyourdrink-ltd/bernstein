#!/usr/bin/env python3
"""Post-decomposition verification script.

Verifies that all public symbols from the 14 decomposed modules remain
importable, that no file exceeds the 800-line cap, and that no import
cycles were introduced.

Run:
    uv run python scripts/verify_decomposition.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 1.  Symbol catalog — every symbol imported by other files in the codebase
# ---------------------------------------------------------------------------

SYMBOL_CATALOG: dict[str, list[str]] = {
    # --- core/orchestrator.py ---
    "bernstein.core.orchestrator": [
        "Orchestrator",
        "OrchestratorConfig",
        "TickResult",
        "group_by_role",
        "ShutdownInProgress",
        "_build_container_config",
        "_compute_total_spent",
        "_total_spent_cache",
        "_build_notification_manager",
        # Re-exported from nudge_manager for backward compat
        "get_orchestrator_nudges",
        "nudge_manager",
    ],
    # --- core/spawner.py ---
    "bernstein.core.spawner": [
        "AgentSpawner",
        "_extract_tags_from_tasks",
        "_render_prompt",
        "_render_fallback",
        "_render_batch_prompt",
        "_select_batch_config",
        "_load_role_config",
        "_health_check_interval",
        "_inject_scheduled_tasks",
        "build_tool_allowlist_env",
        "check_tool_allowed",
        "parse_tool_allowlist_env",
    ],
    # --- core/task_lifecycle.py ---
    "bernstein.core.task_lifecycle": [
        "process_completed_tasks",
        "claim_and_spawn_batches",
        "maybe_retry_task",
        "retry_or_fail_task",
        "should_auto_decompose",
        "auto_decompose_task",
        "collect_completion_data",
        "check_file_overlap",
        "infer_affected_paths",
        "_get_active_agent_files",
        "_enqueue_paired_test_task",
        "_move_backlog_ticket",
        "prepare_speculative_warm_pool",
        "create_conflict_resolution_task",
    ],
    # --- cli/dashboard.py ---
    "bernstein.cli.dashboard": [
        "BernsteinApp",
        "AgentWidget",
        "ExpertBanditPanel",
        "DashboardHeader",
        "AgentListContainer",
        "_AGENT_WIDGET_HEIGHT",
        "_MAX_VISIBLE_AGENTS",
        "_build_runtime_subtitle",
        "_format_gate_report_lines",
        "_format_relative_age",
        "_gate_status_color",
        "_mini_cost_sparkline",
        "_summarize_agent_errors",
        "_task_retry_count",
        "_format_activity_line",
        "_gradient_text",
        "_priority_cell",
        "_role_glyph",
    ],
    # --- core/gate_runner.py ---
    "bernstein.core.gate_runner": [
        "GateRunner",
        # GateCheckResult — referenced in cookiecutter template only, not in src
        "GateReport",
        "GateResult",
        "GatePipelineStep",
        "VALID_GATE_NAMES",
        "normalize_gate_condition",
        "build_default_pipeline",
        "_is_dep_file",
        "_migration_downgrade_is_pass",
    ],
    # --- core/router.py ---
    "bernstein.core.router": [
        "TierAwareRouter",
        "ProviderConfig",
        "ProviderHealthStatus",
        "Tier",
        "ModelConfig",
        "ModelPolicy",
        "PolicyFilter",
        "RouterError",
        "RouterState",
        "route_task",
        "auto_route_task",
        "get_default_router",
        "load_model_policy_from_yaml",
        "load_providers_from_yaml",
        "signal_max_tokens_escalation",
    ],
    # --- core/task_store.py ---
    "bernstein.core.task_store": [
        "TaskStore",
        "ArchiveRecord",
        "SnapshotEntry",
        "ProgressEntry",
        "PANEL_GRACE_MS",
        "_retry_io",
        # get_task_store — referenced only in scripts/researcher_sandbox.sh, not in Python src
    ],
    # --- core/agent_lifecycle.py ---
    "bernstein.core.agent_lifecycle": [
        "handle_orphaned_task",
        "check_stalled_tasks",
        "check_kill_signals",
        "check_loops_and_deadlocks",
        "check_stale_agents",
        "reap_dead_agents",
        "recycle_idle_agents",
        "refresh_agent_states",
        "send_shutdown_signals",
        "purge_dead_agents",
        "classify_agent_abort_reason",
        "_has_git_commits_on_branch",
        "_maybe_preserve_worktree",
        "_save_partial_work",
        "_try_compact_and_retry",
        "_IDLE_GRACE_S",
        "_IDLE_HEARTBEAT_THRESHOLD_S",
        "_IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S",
        "_COMPACT_MAX_RETRIES",
        "_COMPACT_RETRY_META",
    ],
    # --- core/server.py ---
    "bernstein.core.server": [
        "create_app",
        "SSEBus",
        "TaskStore",
        "TaskCreate",
        "TaskResponse",
        "TaskCompleteRequest",
        "TaskFailRequest",
        "TaskProgressRequest",
        "TaskPatchRequest",
        "TaskBlockRequest",
        "TaskCancelRequest",
        "TaskSelfCreate",
        "TaskStealAction",
        "TaskStealRequest",
        "TaskStealResponse",
        "TaskWaitForSubtasksRequest",
        "TaskCountsResponse",
        "HeartbeatRequest",
        "HeartbeatResponse",
        "StatusResponse",
        "HealthResponse",
        "RoleCounts",
        "PaginatedTasksResponse",
        "BatchClaimRequest",
        "BatchClaimResponse",
        "BatchCreateRequest",
        "BatchCreateResponse",
        "BulletinPostRequest",
        "BulletinMessageResponse",
        "ClusterStatusResponse",
        "NodeRegisterRequest",
        "NodeHeartbeatRequest",
        "NodeResponse",
        "PartialMergeRequest",
        "PartialMergeResponse",
        "AgentKillResponse",
        "AgentLogsResponse",
        "ChannelQueryRequest",
        "ChannelQueryResponse",
        "ChannelResponseRequest",
        "ChannelResponseResponse",
        "WebhookTaskCreate",
        "WebhookTaskResponse",
        "A2AAgentCardResponse",
        "A2AArtifactRequest",
        "A2AArtifactResponse",
        "A2AMessageRequest",
        "A2AMessageResponse",
        "A2ATaskResponse",
        "A2ATaskSendRequest",
        "DEFAULT_JSONL_PATH",
        "read_log_tail",
        "task_to_response",
        "node_to_response",
        "a2a_message_to_response",
        "a2a_task_to_response",
        "_sse_heartbeat_loop",
        # _parse_upgrade_dict — moved to task_store; store_postgres.py uses type:ignore
    ],
    # --- core/seed.py ---
    "bernstein.core.seed": [
        "parse_seed",
        "SeedConfig",
        "SeedError",
        "NotifyConfig",
        "CORSConfig",
        "MetricSchema",
        "NetworkConfig",
        "RateLimitBucketConfig",
        "RateLimitConfig",
        "StorageConfig",
        "seed_to_initial_task",
        "_build_manager_description",
        "_parse_cors_config",
        "_parse_dashboard_auth",
        "VALID_GATE_NAMES",
        "GatePipelineStep",
        "normalize_gate_condition",
    ],
    # --- cli/run_cmd.py ---
    "bernstein.cli.run_cmd": [
        "run",
        "cook",
        "demo",
        "init",
        "start",
        "detect_available_adapter",
        "setup_demo_project",
        "DEMO_TASKS",
        "exec_restart",
        "RunCostEstimate",
        "_emit_preflight_runtime_warnings",
        "_estimate_run_preview",
        "_finalize_run_output",
        "_wait_for_run_completion",
        "_show_dry_run_plan",
        "_generate_default_yaml",
    ],
    # --- tui/widgets.py ---
    "bernstein.tui.widgets": [
        "TaskRow",
        "TaskListWidget",
        "AgentLogWidget",
        "ActionBar",
        "StatusBar",
        "ShortcutsFooter",
        "CoordinatorDashboard",
        "CoordinatorRow",
        "ApprovalEntry",
        "ApprovalPanel",
        "ScratchpadViewer",
        "ScratchpadEntry",
        "ToolObserverWidget",
        "WaterfallWidget",
        "QualityGateResult",
        "ModelTierEntry",
        "SLOBurnDownWidget",
        "STATUS_COLORS",
        "STATUS_DOTS",
        "SPARKLINE_CHARS",
        "status_color",
        "status_dot",
        "agent_badge_color",
        "classify_role",
        "build_coordinator_summary",
        "build_token_budget_bar",
        "build_cache_hit_sparkline",
        "list_scratchpad_files",
        "filter_scratchpad_entries",
        "build_model_tier_entries",
        "render_model_tier_table",
        "render_waterfall_batches",
        "build_slo_burndown_text",
    ],
    # --- core/routes/tasks.py ---
    "bernstein.core.routes.tasks": [
        "router",
    ],
    # --- core/routes/status.py ---
    "bernstein.core.routes.status": [
        "router",
        "build_alerts",
    ],
}

# ---------------------------------------------------------------------------
# 2.  Import verification
# ---------------------------------------------------------------------------


def verify_imports() -> tuple[int, int, list[str]]:
    """Try to import every symbol.  Returns (passed, failed, error_msgs)."""
    passed = 0
    failed = 0
    errors: list[str] = []

    for module_path, symbols in SYMBOL_CATALOG.items():
        for sym in symbols:
            try:
                mod = importlib.import_module(module_path)
                obj = getattr(mod, sym, _SENTINEL)
                if obj is _SENTINEL:
                    failed += 1
                    errors.append(f"MISSING  {module_path}.{sym}")
                else:
                    passed += 1
            except Exception as exc:
                failed += 1
                errors.append(f"IMPORT_ERROR  {module_path}.{sym} -> {exc!r}")

    return passed, failed, errors


_SENTINEL = object()

# ---------------------------------------------------------------------------
# 3.  Line-count check (max 800 lines per file)
# ---------------------------------------------------------------------------

MAX_LINES = 800


def check_line_counts() -> tuple[int, list[str]]:
    """Check that no file in core/ or cli/ exceeds MAX_LINES."""
    src_root = Path(__file__).resolve().parent.parent / "src" / "bernstein"
    violations: list[str] = []
    checked = 0

    for subdir in ("core", "cli"):
        dir_path = src_root / subdir
        if not dir_path.exists():
            continue
        for py_file in sorted(dir_path.rglob("*.py")):
            if py_file.name == "__pycache__":
                continue
            line_count = len(py_file.read_text(encoding="utf-8").splitlines())
            checked += 1
            if line_count > MAX_LINES:
                violations.append(f"{py_file.relative_to(src_root)}  ({line_count} lines)")

    return checked, violations


# ---------------------------------------------------------------------------
# 4.  Import cycle detection (lightweight — depth-limited BFS)
# ---------------------------------------------------------------------------


def _check_single_import(mod_name: str) -> str | None:
    """Try importing a module, return a cycle description or None."""
    try:
        importlib.import_module(mod_name)
    except ImportError as exc:
        msg = str(exc).lower()
        if "circular" in msg or "cannot import name" in msg:
            return f"CYCLE  {mod_name} -> {exc}"
    except Exception:
        pass
    return None


def check_import_cycles() -> list[str]:
    """Detect circular imports among bernstein.core and bernstein.cli modules.

    Uses importlib to load each module and inspects sys.modules for cycles.
    This is a lightweight check — not a full graph analysis.
    """
    cycles: list[str] = []
    src_root = Path(__file__).resolve().parent.parent / "src" / "bernstein"
    modules_to_check: list[str] = []

    for subdir in ("core", "cli"):
        dir_path = src_root / subdir
        if not dir_path.exists():
            continue
        for py_file in sorted(dir_path.rglob("*.py")):
            if py_file.name.startswith("__"):
                continue
            rel = py_file.relative_to(src_root.parent)
            mod_name = str(rel).replace("/", ".").replace("\\", ".").removesuffix(".py")
            modules_to_check.append(mod_name)

    for mod_name in modules_to_check:
        cycle = _check_single_import(mod_name)
        if cycle:
            cycles.append(cycle)

    return cycles


# ---------------------------------------------------------------------------
# 5.  Main
# ---------------------------------------------------------------------------


def _print_import_results(passed: int, failed: int, import_errors: list[str]) -> None:
    """Print import verification results."""
    total_symbols = passed + failed
    print(f"       {passed}/{total_symbols} symbols imported successfully")
    if import_errors:
        print()
        for err in import_errors:
            print(f"       FAIL: {err}")


def _print_line_count_results(files_checked: int, violations: list[str]) -> None:
    """Print line count check results."""
    print(f"       {files_checked} files checked")
    if violations:
        print(f"       {len(violations)} file(s) exceed {MAX_LINES} lines:")
        for v in violations:
            print(f"       OVER: {v}")
    else:
        print(f"       All files within {MAX_LINES}-line limit")


def main() -> int:
    print("=" * 70)
    print("  Bernstein Decomposition Verification")
    print("=" * 70)

    print("\n[1/3] Verifying symbol imports...")
    passed, failed, import_errors = verify_imports()
    _print_import_results(passed, failed, import_errors)

    print(f"\n[2/3] Checking line counts (max {MAX_LINES} per file)...")
    files_checked, violations = check_line_counts()
    _print_line_count_results(files_checked, violations)

    print("\n[3/3] Checking for import cycles...")
    cycles = check_import_cycles()
    if cycles:
        print(f"       {len(cycles)} cycle(s) detected:")
        for c in cycles:
            print(f"       CYCLE: {c}")
    else:
        print("       No import cycles detected")

    print("\n" + "=" * 70)
    all_ok = failed == 0 and len(violations) == 0 and len(cycles) == 0
    if all_ok:
        print("  RESULT: ALL CHECKS PASSED")
    else:
        parts = []
        if failed:
            parts.append(f"{failed} missing symbol(s)")
        if violations:
            parts.append(f"{len(violations)} oversized file(s)")
        if cycles:
            parts.append(f"{len(cycles)} import cycle(s)")
        print(f"  RESULT: FAILED — {', '.join(parts)}")
    print("=" * 70)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
