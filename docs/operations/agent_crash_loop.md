# Agent crash loops and the parked state

When an agent fails to spawn, the supervisor does not give up on the
first failure nor retry forever. It applies a bounded respawn budget. A
session that crash-loops past that budget is parked: the supervisor
refuses to respawn it until an operator intervenes. This turns a noisy
crash loop into a single, auditable failure mode.

## TL;DR

| Concept | Default | Notes |
|---------|---------|-------|
| Respawn budget | 3 respawns / 60s window | Initial spawn is not counted |
| Backoff | `500ms * attempt`, capped at `5s` | Linear growth |
| Window reset | Rolling | Respawns older than the window fall out of the count |
| On exhaustion | Session is parked | `AgentStartupExhausted` event published |
| Recovery | Operator-driven only | `bernstein agents resume <id>` |
| Where parks live | `.sdd/runtime/spawn_supervisor/parked.json` | Read by the CLI, the TUI and `bernstein fleet` |

## How the budget works

1. The first spawn of a session is the initial spawn. It never consumes
   budget.
2. Every failed respawn inside the rolling window consumes one unit of
   budget and waits the linear backoff before retrying.
3. Backoff is `initial_backoff_ms * attempt`, capped at `max_backoff_ms`.
   With defaults that is 500ms, 1000ms, 1500ms, ... up to 5000ms.
4. The window is rolling. A session that recovers and stays up long
   enough for old respawn timestamps to age out of the window regains
   its full budget without any operator action.
5. When the number of respawns inside the window reaches `max_respawns`,
   the next failure parks the session.

## The parked state

A parked session is terminal until resumed. The supervisor:

- transitions the session to `parked`;
- publishes a single `agent.startup_exhausted` lifecycle event carrying
  `reason`, `last_error`, `attempts`, `window_seconds`, and
  `max_respawns`;
- refuses any further spawn with `SessionParkedError`.

The park reason is always `respawn_budget_exhausted`. The persistent
crash loop almost always means a real fault: a missing adapter binary,
bad configuration, or an expired token. Read `last_error` first.

## Where parked state is kept

Parks are written to `.sdd/runtime/spawn_supervisor/parked.json` under
the project root, and that file — not process memory — is what the
operator surfaces read.

This matters because the process that parks a session is the
orchestrator, and `bernstein agents parked` is a different process that
starts with an empty supervisor. Reading only in-memory state made the
surfaces report "nothing parked" unconditionally, whatever had happened
(#3453).

The store is rewritten on every supervision state change, including a
clean spawn. That is deliberate: it lets a reader tell the two zeros
apart.

| What you see | What it means |
|---|---|
| `No parked sessions.` | The store exists and is empty — a measured zero |
| `Parked state unavailable.` | No supervisor has ever written to this workspace — not a report of zero |

A supervisor is authoritative only for the sessions it knows about, so
writing the store merges rather than overwrites: a second process
holding its own supervisor cannot erase parks it did not make.

## Inspecting parked sessions

```
bernstein agents parked      # list parked sessions
bernstein agents parked --workdir /path/to/project
bernstein ps                 # running agents, with a parked footer
```

Both read the on-disk store unioned with the calling process's own
supervisor, so they agree.

`bernstein ps --json-output` keeps its existing shape — a bare agent
list, or an object when something is parked — and the object now carries
`parked_available` alongside `parked`. The unavailable-versus-empty
distinction is therefore on the human surfaces and on `bernstein agents
parked`; `ps --json` cannot express it without changing its top-level
type, which would break existing readers.

## What gets parked, and under what id

The supervisor budgets *respawns*, so its key has to survive the retries
it is counting. A spawn session id cannot: the spawner mints a fresh
`<role>-<uuid>` per attempt, and a retry mints a new task id as well
(#2806), so either would make every failure look like a first failure
and nothing would ever reach exhaustion.

Parks made by the orchestrator are therefore keyed on the batch's
lineage — the same key its own consecutive-failure counter uses — and
render as `batch:<lineage-ids>`. Pass that id verbatim to `resume`.

## Resuming a session

After fixing the root cause, reset the budget and clear the parked
state:

```
bernstein agents resume batch:T-1234
```

Resume is the only recovery path. There is no automatic remediation on
park; that is intentional, so an operator confirms the fault is gone
before the session is allowed to spawn again. Resuming clears the
respawn window, so the session starts again with a full budget, and
removes the id from the on-disk store — including when the parking
process has already exited.

## Tuning the budget

`RespawnBudget` accepts `max_respawns`, `window_seconds`,
`initial_backoff_ms`, and `max_backoff_ms`. Widen the window or raise
the ceiling for environments with known transient flakiness; tighten
them where a fast park is preferable to repeated retries.
