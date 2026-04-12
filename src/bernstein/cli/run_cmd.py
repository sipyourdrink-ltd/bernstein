"""Run commands: init, conduct, downbeat (legacy start), and the main CLI group.

This module is a thin re-export shim.  The actual implementations live in:
- ``run_preflight`` — cost estimation, preflight checks, quality-gate bypass
- ``run_bootstrap`` — main Click commands, plan helpers, execution bootstrap
- ``run_confirm``   — recipe/cook commands, demo command, confirmation helpers
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

# --- run_bootstrap ---
from bernstein.cli.run_bootstrap import _build_synthetic_plan as _build_synthetic_plan
from bernstein.cli.run_bootstrap import _default_constraints_for as _default_constraints_for
from bernstein.cli.run_bootstrap import _detect_project_type as _detect_project_type
from bernstein.cli.run_bootstrap import _generate_default_yaml as _generate_default_yaml
from bernstein.cli.run_bootstrap import _load_dry_run_tasks as _load_dry_run_tasks
from bernstein.cli.run_bootstrap import _load_plan_goal as _load_plan_goal
from bernstein.cli.run_bootstrap import _save_plan_markdown as _save_plan_markdown
from bernstein.cli.run_bootstrap import _show_dry_run_plan as _show_dry_run_plan
from bernstein.cli.run_bootstrap import _wait_for_run_completion as _wait_for_run_completion
from bernstein.cli.run_bootstrap import exec_restart as exec_restart
from bernstein.cli.run_bootstrap import init as init
from bernstein.cli.run_bootstrap import run as run
from bernstein.cli.run_bootstrap import start as start

# --- run_confirm ---
from bernstein.cli.run_confirm import DEMO_TASKS as DEMO_TASKS
from bernstein.cli.run_confirm import RecipeStage as RecipeStage
from bernstein.cli.run_confirm import _completed_sprints as _completed_sprints
from bernstein.cli.run_confirm import _extract_recipe_stages as _extract_recipe_stages
from bernstein.cli.run_confirm import _print_cook_dry_run as _print_cook_dry_run
from bernstein.cli.run_confirm import _print_demo_summary as _print_demo_summary
from bernstein.cli.run_confirm import _stop_demo_processes as _stop_demo_processes
from bernstein.cli.run_confirm import _wait_for_recipe_completion as _wait_for_recipe_completion
from bernstein.cli.run_confirm import cook as cook
from bernstein.cli.run_confirm import demo as demo
from bernstein.cli.run_confirm import detect_available_adapter as detect_available_adapter
from bernstein.cli.run_confirm import setup_demo_project as setup_demo_project

# --- helpers re-exports (used by mock.patch in tests) ---
import time as time

from bernstein.cli.helpers import server_get as server_get

# --- run_preflight ---
from bernstein.cli.run_preflight import RunCostEstimate as RunCostEstimate
from bernstein.cli.run_preflight import _configure_quality_gate_bypass as _configure_quality_gate_bypass
from bernstein.cli.run_preflight import _emit_preflight_runtime_warnings as _emit_preflight_runtime_warnings
from bernstein.cli.run_preflight import _estimate_run_preview as _estimate_run_preview
from bernstein.cli.run_preflight import _finalize_run_output as _finalize_run_output
from bernstein.cli.run_preflight import _make_profile_ctx as _make_profile_ctx
from bernstein.cli.run_preflight import _quiet_bootstrap_console as _quiet_bootstrap_console
from bernstein.cli.run_preflight import _show_run_summary as _show_run_summary
from bernstein.cli.run_preflight import _try_fallback_display as _try_fallback_display
