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
