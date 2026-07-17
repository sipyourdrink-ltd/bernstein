# Fleet mode

`bernstein fleet` is a supervisory dashboard that aggregates state from
multiple Bernstein projects into a single view. It is for the team or
org that runs Bernstein on, say, six repositories at once and wants
*one* place to look at run state, agent count, queued approvals, last
commit SHA, and 7-day cost - without giving up the per-project
deterministic guarantees.

The fleet aggregator is purely a fan-out reader plus a dispatcher for
bulk actions (`core/fleet/__init__.py:1`); it does not hold any
orchestration state itself. Each project keeps its own task server,
WAL, and `.sdd/` tree, and the fleet view is rebuilt by polling them.

---

## What fleet mode is

A fleet aggregates per-project state from a list of locally-running
task servers configured in `~/.config/bernstein/projects.toml` (the
default returned by `default_projects_config_path()`,
`core/fleet/config.py`). Per project, the aggregator surfaces:

- **State** - `INITIALIZING` / `ONLINE` / `DEGRADED` / `OFFLINE` /
  `PAUSED` (`core/fleet/aggregator.py:33`).
- **Run state** - the plain-language phase the task server reports
  (`core/fleet/aggregator.py:155-159`).
- **Agents** - count of live agents and the sorted list of roles
  currently working (`core/fleet/aggregator.py:120-134`).
- **Approvals** - number of pending approvals queued
  (`core/fleet/aggregator.py:136-141`).
- **Last SHA** - last 12 chars of the last known commit SHA
  (`core/fleet/aggregator.py:143-148`).
- **Cost (7d)** - rolling 7-day spend in USD plus a sparkline of the
  last seven daily samples (`core/fleet/cost_rollup.py`).
- **Audit chain** - `ok` / `BROKEN` (HMAC chain verification result,
  `core/fleet/audit.py`).

Two views render this state: a Textual TUI (default) and a FastAPI web
view (with `--web HOST:PORT`).

---

## `bernstein fleet` group

The CLI is wired in `cli/commands/fleet_cmd.py:50` (`@click.group("fleet",
invoke_without_command=True)`). With no subcommand, it launches the
TUI (or web view if `--web` is set). Subcommands cover the bulk
actions and a non-interactive list view.

### Top-level: launch the dashboard

```
bernstein fleet [--config PATH] [--web HOST:PORT]
```

- `--config PATH` - point at a non-default fleet config file. Default
  is `~/.config/bernstein/projects.toml`.
- `--web HOST:PORT` - run the web view (FastAPI + uvicorn) instead of
  the TUI. Bind format accepts `:8080` (binds to 127.0.0.1:8080),
  `0.0.0.0:8080`, or `8080` (`fleet_cmd.py:139-146`).

When `uvicorn` is not installed, `--web` exits with a clear message
(`fleet_cmd.py:156-160`). Without `--web`, the TUI is rendered via
`textual` if available, falling back to a Rich table otherwise
(`fleet_cmd.py:80-136`).

### `bernstein fleet ls`

```
bernstein fleet ls
```

Prints a Rich table of configured projects (name, path, task-server
URL) without launching the dashboard
(`fleet_cmd.py:287-299`). Use this to confirm `projects.toml` is
parsing correctly. Any per-project parse warnings appear after the
table (`fleet_cmd.py:42-48`).

### Bulk subcommands

All bulk subcommands accept `--names <name>` (repeat for multiple
projects) and `--filter <expression>` (e.g. `cost>5`) to restrict the
target list. Without filters, every configured project is targeted.
The selection logic - including filter expression parsing - lives in
`core/fleet/bulk.py:select_projects` (`fleet_cmd.py:183-211`).

```
bernstein fleet bulk-stop          [--names …] [--filter …]
bernstein fleet bulk-pause         [--names …] [--filter …]
bernstein fleet bulk-resume        [--names …] [--filter …]
bernstein fleet bulk-cost-report   [--names …] [--filter …]
```

- **`bulk-stop`** - invoke each project's `bernstein stop` via its CLI
  (`fleet_cmd.py:223-236`).
- **`bulk-pause`** - stop the project's daemon (`fleet_cmd.py:239-252`).
- **`bulk-resume`** - restart the project's daemon
  (`fleet_cmd.py:255-268`).
- **`bulk-cost-report`** - run `bernstein cost report` against every
  selected project and emit a JSON envelope per project
  (`fleet_cmd.py:271-284`).

Output is a compact JSON blob: `{"action": "...", "succeeded": [...],
"failed": {project: error_message}}` (`fleet_cmd.py:214-220`).

---

## Aggregator: where state lives, refresh cadence

The aggregator (`core/fleet/aggregator.py:171`) owns one
`httpx.AsyncClient` and, per project, two background workers:

- A **status poller** that fetches `/status` on a configurable
  interval (`poll_interval_s`, default `2.0` seconds). Each pass
  derives a `ProjectSnapshot` from the response
  (`core/fleet/aggregator.py:110-168`).
- An **SSE worker** that subscribes to the project's `/events` stream
  and merges every event into a single shared async queue
  (`core/fleet/aggregator.py:217`, `_sse_loop`).

Snapshots are mutable per project but exposed as deep copies via
`snapshots()` (`core/fleet/aggregator.py:262-280`), so the dashboard
cannot mutate live state.

A project that cannot be reached transitions to `OFFLINE` and is
retried with exponential backoff between `backoff_min_s = 1.0` and
`backoff_max_s = 30.0` (`core/fleet/aggregator.py:184-211`). One
unreachable project never blocks updates for the others.

The HTTP timeout (`http_timeout_s`, default `5.0` seconds) is set
deliberately low so a hung task server never blocks another row's
update (`core/fleet/aggregator.py:200-202`).

Cost rollups are sourced from each project's on-disk cost history via
`rollup_costs(...)` (`core/fleet/cost_rollup.py`), keeping the last
seven daily samples by default (`cost_window_days = 7`).

Audit-chain verification is delegated to
`core/fleet/audit.check_audit_tail`, which reads the project's HMAC-chained
audit log and reports `ok` or `BROKEN`. A `BROKEN` indicator means an
operator must investigate the audit tail directly - see
`security/AUDIT.md`.

---

## Dashboard: TUI vs web

### TUI (default)

`bernstein fleet` (no subcommand) builds a Textual app via
`build_textual_app(aggregator, config)` (`fleet_cmd.py:93-98`). The
columns are `Project`, `State`, `Run`, `Agents`, `Approvals`,
`Last SHA`, `Cost (7d)`, `Sparkline`, `Chain`. When Textual is not
installed, the CLI falls back to a static Rich table render
(`fleet_cmd.py:105-136`).

### Web view

`bernstein fleet --web 0.0.0.0:8080` boots a FastAPI app via
`build_fleet_app(aggregator, config)` and runs it under uvicorn
(`fleet_cmd.py:149-175`). The app exposes the same snapshot data plus
a `/events` SSE stream so dashboards and tools can subscribe to fleet
changes. `--web` requires `uvicorn` to be importable.

Both views read the same in-memory `FleetAggregator` instance, so the
TUI and the web view see identical numbers.

---

## Adding / removing projects

Projects are declared in `~/.config/bernstein/projects.toml`. Each
entry needs at minimum a `name`, a `path` (the Bernstein-managed
working tree), and a `task_server_url` (where the task server
listens). The full schema lives in
`core/fleet/config.py:ProjectConfig`.

Workflow:

1. Stand up the project the normal way (`bernstein init`,
   `bernstein run`, or `bernstein daemon install`).
2. Add a `[[project]]` block to `projects.toml`.
3. Run `bernstein fleet ls` to confirm the parse succeeded.
4. Run `bernstein fleet` (or `--web …`) and the new project shows up
   on the next poll cycle (≤2 seconds by default).

Removing a project is the reverse: stop its task server (or use
`bernstein fleet bulk-pause --names <project>`), remove the
`[[project]]` block, and re-run `bernstein fleet ls`.

Per-project parse errors do not crash the dashboard. They surface as
`config global:` or `config project[N]:` warnings via
`_print_config_errors` (`fleet_cmd.py:42-48`).

---

## Multi-tenancy notes

Fleet mode is multi-project, **not** multi-tenant in the security
sense. Every task server it queries is assumed to be run by the same
operator, on a network the operator trusts. The fleet HTTP client
holds no per-project credential, and the aggregator does not
authenticate against the task server beyond what `httpx` does by
default.

If you need actual tenant isolation:

- Run each tenant's projects under a separate fleet config file and a
  separate fleet process.
- Apply network-level isolation between tenants (firewall rules, mesh
  policy).
- Confirm each project enforces its own auth on `/status`, `/events`,
  and the bulk-action surface (see `security/security-hardening.md`).

For request-time permission enforcement inside a single project, see
[`architecture/permission-modes.md`](../architecture/permission-modes.md).

---

## Code pointers

- `cli/commands/fleet_cmd.py:50` - `@click.group("fleet",
  invoke_without_command=True)`.
- `cli/commands/fleet_cmd.py:80-102` - `_run_tui` (Textual app + Rich
  fallback).
- `cli/commands/fleet_cmd.py:139-175` - `--web HOST:PORT` parsing and
  uvicorn boot.
- `cli/commands/fleet_cmd.py:223-284` - `bulk-stop`, `bulk-pause`,
  `bulk-resume`, `bulk-cost-report`.
- `cli/commands/fleet_cmd.py:287-299` - `ls`.
- `core/fleet/__init__.py:1` - public surface (re-exports).
- `core/fleet/aggregator.py:33` - `ProjectState` enum.
- `core/fleet/aggregator.py:43-90` - `ProjectSnapshot` dataclass.
- `core/fleet/aggregator.py:171` - `FleetAggregator` (lifecycle: start
  / snapshots / events / stop).
- `core/fleet/aggregator.py:184-218` - aggregator constructor (poll
  interval, HTTP timeout, backoff bounds, cost window).
- `core/fleet/config.py` - `ProjectConfig`,
  `default_projects_config_path()`, `load_projects_config()`.
- `core/fleet/bulk.py` - `select_projects`, `bulk_stop`, `bulk_pause`,
  `bulk_resume`, `bulk_cost_report`.
- `core/fleet/cost_rollup.py` - `rollup_costs`, `CostSparkline`.
- `core/fleet/audit.py` - `check_audit_tail`, `AuditChainStatus`.
- `core/fleet/prometheus_proxy.py` - `merge_prometheus_metrics` (for
  Grafana / scrape integration).
- `core/fleet/tui.py` - `build_textual_app`, `build_rows`,
  `format_footer`.
- `core/fleet/web.py` - `build_fleet_app` (FastAPI factory).

---

## Web UI: multi-project grid view (PR #1338)

The web view ships a grid that pairs with the topbar `Single ↔ Fleet`
toggle. The toggle is sticky in `localStorage` and accepts a `?fleet=1`
URL parameter so deep links and back / forward navigation behave.

- `/ui/fleet` - grid; one card per project showing health icon, active
  agents, today's spend, pending approvals.
- Clicking a card drills into `/tasks?project=<slug>` while preserving
  the fleet flag so the topbar returns the operator to the grid.
- Cross-project search syntax: `agent:claude status:running across:all`
  with live filter chips.

Backend stubs that land first and harden behind the same routes:

- `GET /api/v1/fleet/projects` - per-project snapshot list.
- `GET /api/v1/fleet/search` - cross-project query.

Both endpoints respond with an empty list + `{"stub": true}` until a
`FleetAggregator` is attached to the process.

Cache-key hygiene: every fleet-mode query is namespaced under
`['fleet', ...]`; existing single-project keys are untouched, so a
mode flip never invalidates the wrong cache.

## Named sandbox pools

Where fleet-mode aggregates discovery across projects, [named sandbox
pools](sandbox-pools.md) govern where cluster work may land and which knobs
recipe authors may turn. A worker joins a pool with `bernstein worker --pool
<name> --server <url>`, signing an Ed25519 enrolment receipt that binds its
install identity to the pool, so the execution host of every subsequent claim is
cryptographically attributable and offline-verifiable.
