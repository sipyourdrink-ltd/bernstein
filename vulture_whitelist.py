# Vulture whitelist — false positives that are actually used
# These are parameters required by framework signatures (Click, signal handlers, etc.)

# Signal handler signature requires 'frame' parameter
frame  # noqa
# Click callback parameters bound by decorator, not called directly
parameters  # noqa
formatter  # noqa
headless  # noqa
# Trigger source interface requires 'raw_event'
raw_event  # noqa
# Used at runtime via string reference
_Literal  # noqa
# Pre-import for startup speed (import side-effects warm caches)
bernstein.cli.run_cmd  # noqa
bernstein.core.bootstrap  # noqa
# Parameter used by callers
bold  # noqa
# Called dynamically or exported for external use
_strip_hash  # noqa
_render_figlet_raw  # noqa
_run_animated  # noqa
_print_static  # noqa
_agents_from_dicts  # noqa
# Parameter kept for API consistency (callers may pass it in future)
qualified_prefix  # noqa
# Claude Code Routine adapter — public API not yet wired into orchestrator
RoutineCostTracker  # noqa
build_fire_payload  # noqa
build_fire_headers  # noqa
build_fire_url  # noqa
parse_fire_response  # noqa
select_trigger  # noqa
normalize_routine_webhook  # noqa
# Dataclass fields and interface params used by callers
fired_at  # noqa
enabled  # noqa
poll_interval_seconds  # noqa
max_wait_minutes  # noqa
branch_prefix  # noqa
session_id  # noqa
session_url  # noqa
check_budget  # noqa
record_fire  # noqa
extract_github_context  # noqa
list_scenarios  # noqa
get_scenario_detail  # noqa
