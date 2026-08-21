## Problem

`system_addendum` is the channel `adapter.spawn()` uses to carry protocol-critical instructions - how an agent reports completion and how it heartbeats - into the running process. The primary spawn path (`spawn_for_tasks` / `_spawn_for_tasks_internal`) resolves this per-spawn (task metadata > role policy > seed default > `"balanced"`) and forwards it. Four other call sites did not:

- `spawn_for_resume()` - the crash-recovery path goes straight to `self._adapter.spawn()` without going through `_spawn_for_tasks_internal`, and never resolved a style addendum at all, so it always sent `system_addendum=""`.
- `_spawn_in_container()`, `_spawn_in_sandbox()`, and `_spawn_via_sandbox_session()` - each has a direct-subprocess fallback for when the container/sandbox fails to start. The fallback calls `adapter.spawn()` but dropped the addendum the caller had already resolved.

All four of these are live-agent paths where the agent still has to report completion and heartbeat, so an agent resumed after a crash, or downgraded to a host subprocess when its container/sandbox couldn't start, could run to completion and never be seen to finish.

## Fix

- `spawn_for_resume()` now resolves the response style the same way the primary path does (`resolve_response_style` + `render_style_addendum`, using the same role-policy lookup the resume path already computed) and forwards the rendered addendum via `system_addendum`.
- `_spawn_in_container()`, `_spawn_in_sandbox()`, and `_spawn_via_sandbox_session()` now take a `system_addendum` parameter and forward it in their fallback `adapter.spawn()` call. The three call sites in `_spawn_for_tasks_internal` that invoke these helpers now pass the `style_addendum` already resolved earlier in that method - one source, several consumers, rather than each site recomputing it.

## Not changed

The primary container/sandbox path (when the container/sandbox actually starts) never calls `adapter.spawn()` at all - it builds a raw shell command via `_adapter_cmd_for_container()` and runs it directly inside the container. That path has no parameter to carry `system_addendum` through today; giving it one means picking a per-adapter injection mechanism (real system-prompt flag vs. prompt-append vs. unsupported), which is a larger change than this fix and is left for follow-up.

## Tests

- `test_spawn_for_resume_forwards_system_addendum` - drives `spawn_for_resume` with a role policy set to `terse` and asserts the adapter received the matching rendered addendum (was `KeyError` before the fix, since the kwarg wasn't passed at all).
- `test_spawn_in_sandbox_fallback_forwards_system_addendum`
- `test_legacy_container_fallback_forwards_system_addendum`
- `test_provisioning_failure_fallback_forwards_system_addendum`

Each of the three fallback tests calls the private helper directly with a non-empty `system_addendum` and asserts the fake adapter's `spawn()` received it (all four failed before the fix - three with `TypeError: unexpected keyword argument`, the resume one with `KeyError`).

Verified:
```
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pytest tests/unit/test_crash_recovery.py tests/unit/test_spawner_sandbox.py \
  tests/unit/test_spawner_explicit_container_runtime.py tests/unit/test_spawner_sandbox_session.py \
  tests/unit/agents/test_spawner_response_style.py tests/unit/agents/ tests/unit/adapters/ -q
```
All clean; 972 passed in `tests/unit/agents/` + `tests/unit/adapters/`, 208 passed in the spawner-focused run.

Closes #3565
