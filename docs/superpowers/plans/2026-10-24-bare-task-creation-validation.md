# Bare-task creation validation

Base: `62aaed0c5` (fresh origin/main). Isolated worktree:
`/tmp/bernstein-0004-bare-task`, branch `codex/0004-bare-task`.
The dirty source checkout was not edited or copied.

Commands below use the existing interpreter without installing dependencies:

```sh
export PYTHONPATH="$PWD/src"
PY=/Users/sasha/code/products/bernstein/bernstein/.venv/bin/python
BIN=/Users/sasha/code/products/bernstein/bernstein/.venv/bin
```

## RED evidence

`$PY -m pytest tests/unit/test_task_claim_receipt_route.py -k http_created -q`
failed before implementation: POST /api/v1/tasks returned task `06138124cfda`,
but POST /tasks/claim-receipt returned `taskId: ""`. The creation route writes
TaskStore while the receipt route read only task-backlog.json. Creation and MCP
wrapping already existed on this base, so they were reused.

## Focused verification

`$PY scripts/run_tests.py tests/unit/test_task_claim_receipt_route.py tests/unit/tenant_scope/test_bare_task_claim.py tests/unit/mcp/test_advertised_schema_parity.py tests/unit/test_openapi_snapshot_drift.py`
passed all four files: 15 + 3 + 38 + 6 tests, 78.4 seconds.

Additional isolated runs passed:

- `tests/unit/test_auth_middleware_claim_next_scope.py`: 12 tests.
- `tests/unit/mcp/test_claim_update_tools.py`: 6 tests.

The lifecycle tests use real ASGI HTTP and MCP calls. Planner/spawn sentinels
fail on invocation. Dependency tests reject caller-supplied completion of an
unfinished task. Auth-enabled tests verify tenant/role/task scope, fail-closed
empty worker scope, and absence of unauthorized IDs from receipt snapshots.
HMAC tests verify creation fields and detect modified audit bytes. Store
claims preserve task metadata, order by priority and do not write backlog rows.

## Repository checks

- `$BIN/ruff check src/` and changed tests: PASS.
- `$BIN/ruff format --check src/`: PASS (2156 files already formatted).
- `$BIN/lint-imports`: PASS (3 contracts kept, 0 broken).
- `$PY scripts/check_routes_broad_except.py`: PASS.
- `$PY scripts/check_unreachable_controls.py`: PASS (420 known symbols).
- `$PY scripts/generate_openapi.py`: regenerated docs/reference/openapi.json;
  existing duplicate operation-ID warnings only; snapshot drift tests PASS.
- `$BIN/bernstein agents-md sync`: executed twice, generated output unchanged.
  The generated module map indexes core/tasks as a package. Architecture notes
  for the added helper live in docs/sdd/bare-task-claims.md.
- `$BIN/bernstein readme-l10n sync` and `verify`: PASS, all 23 locales. The
  command-only documentation table row is identical in every locale.
- `$BIN/pyright src/bernstein/core/tasks/store_claim.py src/bernstein/core/routes/task_claim_receipt.py`:
  PASS, zero errors.
- `$BIN/mypy src`: 10 errors in 7 untouched files (core/__init__.py loader
  types, govern/probe_runtime lambda, adapters/caching_adapter property,
  cli/commands/telemetry_cmd callable, adapters/crew_ingest duplicate variable,
  eval/bench/gate_evasion_baseline sort types, cli/commands/status_cmd optional
  mapping). No errors in changed files.

A broad Pyright run without explicit --pythonpath resolved the worktree's
absent local environment and reports unresolved dependencies/legacy runtime
redirect imports. This is not evidence of a clean repository-wide type check.
A corrected explicit-interpreter run is recorded in the final addendum below.
Full isolated-suite execution is coordinated by the parent; no bare pytest
suite was run.

## Final addendum

After adding a true default-app audit regression, the isolated route file
passed **16 tests** (83.9 seconds). This test constructs `create_app` with its
own temporary audit key, leaves `app.state.audit_chain` untouched, and verifies
the creation event after independently reopening the chain.

`$BIN/pyright --pythonpath "$PY" src/bernstein/core/tasks/store_claim.py src/bernstein/core/routes/task_claim_receipt.py src/bernstein/core/routes/task_crud.py src/bernstein/mcp/server.py`
completed with 413 diagnostics, dominated by existing lazy redirect imports,
unknown inferred types, private members and MCP experimental deprecations in
the two existing large modules. Full output is retained locally at
`/tmp/bernstein-0004-pyright-changed.log`. The two added/updated claim helper
modules pass the separately scoped zero-error check. The original full-src
Pyright invocation is still running; it is not treated as a passing gate.
No claim is made that repository-wide type checking is clean.

## Parent-coordinated full-suite baseline

`$PY scripts/run_tests.py -x` completed after 655.8 seconds with 108 files
passing and 1 file failing. The failure was
`tests/unit/cli/test_advanced_cmd_trace.py::test_trace_export_needs_trace_extra`:
it expects `The trace extra is required`, but receives
`No such run: test-run-123`. Parent independently reproduced this exact failure
on clean detached base `62aaed0c5` in `/tmp/bernstein-0004-baseline` with the same
interpreter and base-specific PYTHONPATH (1 failed, 1 warning, 3.00 seconds).
The unrelated CLI behavior was not changed. The full suite is not reported green.

The original full-src Pyright run without explicit --pythonpath was terminated
with SIGTERM on its own wrapper/node PIDs after the corrected explicit run
completed. It never produced a completed result and is not a passing gate.

## Quality-review concurrency follow-up

The quality review identified a legitimate stale-candidate removal case:
`create_gate_with_edges` exposes a staged gate while holding the store lock,
then removes that gate if journal flush fails. A receipt claimer that selected
the gate while awaiting the same lock must skip its disappearance.

The new deterministic test uses the real TaskStore, actual asyncio lock,
actual gate rollback and an injected journal-flush failure; CAS is not mocked.
Before the fix, `$PY scripts/run_tests.py tests/unit/tasks/test_store_claim.py`
failed with `KeyError: 'vanishing-gate'` at `claim_by_id` (1 failure, 1 pass).
The bounded fix catches `KeyError` alongside existing `ValueError` and moves
to the next candidate. The parameterized test covers next-task and refusal
results. A competing-claims test queues two actual lock contenders and
requires exactly one acquisition of the same task.

GREEN: `$PY scripts/run_tests.py tests/unit/tasks/test_store_claim.py tests/unit/test_task_claim_receipt_route.py`
passed 3 + 16 tests (20.3 seconds). Ruff and formatting checks for both
follow-up files pass. Explicit-interpreter Pyright over store_claim.py and
task_claim_receipt.py passes with zero errors, warnings or information items.
