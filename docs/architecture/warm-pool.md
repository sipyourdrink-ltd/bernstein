# Warm Pool

**Why does the second agent spawn faster than the first?**

Spawning an agent is not free: Bernstein has to create a git worktree
(5–15 s on a non-trivial repo), optionally start an MCP server process,
and warm up the adapter's CLI. The **warm pool** pays that cost ahead of
time. It tracks a small number of `PoolSlot` records. A ready record may be
backed by a pre-created worktree and (optionally) a pre-started MCP process,
but `ready` does not mean the worktree is provisioned. Today the orchestrator
adds speculative ready records with an empty worktree path; when the worker
launcher claims one, it releases the unprovisioned record and creates a new
worktree instead of reusing one.

If you only have time for one sentence: **the pool tracks slot records, and a
provisioned worktree slot can let the spawner skip `git worktree add`; today,
however, the orchestrator adds speculative records with empty paths, so the
worker launcher creates a new worktree and takes the cold path**. The rest of
this page explains the slot lifecycle, the configuration surface, and the
current wiring.

---

## What is a worktree slot?

A **worktree slot** is a `PoolSlot` record with its own `slot_id`, a target
role, a `worktree_path`, and a `ready → claimed → expired` lifecycle. `ready`
is a status, not a guarantee that a worktree exists: a **provisioned** slot
has a worktree at its path (and may have a pre-started MCP server process),
while an unprovisioned slot has an empty path. Today, the orchestrator adds
speculative ready records with empty paths. When the worker launcher claims
one, it releases that record and creates a new worktree instead of using the
slot as a pre-provisioned worktree. A provisioned ready slot can skip the
5–15 s `git worktree add` cold-start.

`WarmPoolConfig.max_slots` caps the total number of slots ever added to the
pool (ready, claimed and expired all count, and slots are never removed), not
the number idle at once or the number of agents that can run concurrently.
Worktree slots are about spawn latency. They are distinct from the
concurrency slots that adaptive parallelism manages (how many agents
run at once) and from cluster `--slots` (how many workers a remote node
offers).

---

## The problem: agent cold-start latency

A fresh spawn does five sequential things:

1. Resolve adapter + role + model config.
2. `git worktree add .sdd/worktrees/<session>` → typically 5–15 s on a
   real repo (clone hardlinks + checkout).
3. Optionally start an MCP server subprocess and wait for it to register.
4. Warm up the CLI process for the adapter (Claude / Codex / Gemini ...).
5. Stream the first prompt.

Step 2 dominates, especially in CI where workspaces are new and the
filesystem cache is cold. On hot paths (chains of small tasks each
finishing in 30 s) the worktree creation can be more expensive than the
agent run itself.

---

## The solution: a pool of pre-provisioned slots

`core/agents/warm_pool.py` (`WarmPool` class) maintains a small list of
`PoolSlot` objects. Each slot owns:

- A unique `slot_id`.
- A target `role` (`backend`, `qa`, ...).
- A pre-created `worktree_path` on disk.
- An optional `mcp_pid` for a pre-started MCP server process.
- A `created_at` timestamp (for TTL expiry).
- A status: `ready` → `claimed` → `expired`.

When `AgentSpawner._spawn_for_tasks_internal()` needs a worktree it calls
`self._warm_pool.claim_slot(role)` first (skipped on fresh-context retries). On a hit, the pre-built
worktree path is reused and the slow `git worktree add` is skipped:

```python
warm_entry = warm_pool.claim_slot(role) if warm_pool is not None and not fresh_restart_on_retry else None
if warm_pool is not None and warm_entry is not None and not warm_entry.worktree_path:
    warm_pool.release_slot(warm_entry.slot_id)  # unprovisioned slot: go cold
    warm_entry = None
if warm_entry is not None:
    spawn_cwd = Path(warm_entry.worktree_path)
    self._worktree_paths[session_id] = spawn_cwd
    self._worktree_roots[session_id] = worktree_repo_root
    self._warm_pool_entries[session_id] = warm_entry
    logger.info(
        "Using warm pool slot %s for session %s (role=%s)",
        warm_entry.slot_id,
        session_id,
        role,
    )
else:
    spawn_cwd = worktree_mgr.create(session_id)  # cold path
```

Source: `core/agents/spawner_core.py`.

On a miss, the spawner falls back to the cold path (`worktree_mgr.create`)
and the agent eats the 5–15 s - but the cold path remains the safe
default, so the warm pool only ever speeds things up.

Slot fill is **external** to the pool itself: the pool just claims,
releases, and expires. The only caller of `pool.add_slot(slot)` today is
`prepare_speculative_warm_pool` (`core/tasks/task_lifecycle.py`), which adds
slots with `worktree_path=""`; the spawner treats a slot without a
worktree as unprovisioned, releases it, and takes the cold path.

---

## Pool sizing

`WarmPoolConfig` and `load_warm_pool_config()` read a `warm_pool:` section
of `bernstein.yaml` in this shape:

```yaml
warm_pool:
  max_slots: 5             # default: 3
  slot_ttl_seconds: 600    # default: 300 (5 min)
  roles:
    - backend
    - qa
```

| Key | Default | Meaning |
|-----|---------|---------|
| `max_slots` | `3` | Cap on the total number of slots ever added (ready, claimed and expired all count; slots are never removed). The pool silently rejects `add_slot` calls that would exceed this |
| `slot_ttl_seconds` | `300.0` | A `ready` slot older than this is moved to `expired` by `expire_stale()` so the worktree can be reaped |
| `roles` | `[]` | Parsed but not used: claims match the role stored on each slot. |

Source: `WarmPoolConfig` and `load_warm_pool_config` in `warm_pool.py`.
Nothing calls the loader yet: the orchestrator builds the pool with
`WarmPoolConfig(max_slots=max(1, min(2, seed.max_agents)))` and default TTL
and roles, so the `warm_pool:` section has no effect on a run today.

Rule of thumb: `max_slots ≈ peak concurrent spawns of one role`.
Over-provisioning costs disk and inodes; under-provisioning means hot
paths fall back to cold spawns.

---

## Lifecycle of a slot

```
       add_slot()
            │
            ▼
        ┌────────┐  TTL expires    ┌─────────┐
        │ ready  ├────────────────▶│ expired │
        └────┬───┘                 └─────────┘
             │ claim_slot(role)         ▲
             ▼                          │
        ┌─────────┐  release_slot()     │
        │ claimed ├─────────────────────┘
        └─────────┘
```

The transitions are the methods on `WarmPool`:

- **`add_slot(slot)`** - append a freshly-built `PoolSlot` to the pool.
  Beyond `max_slots`, additions are silently ignored.
- **`claim_slot(role)`** - return the first `ready` slot with that role in
  insertion order, marking it `claimed`. It does not check the slot's age;
  even an over-TTL slot is claimable until an explicit expiry sweep marks it
  `expired`. Returns `None` if no matching ready slot exists; the spawner
  falls back to the cold path.
- **`release_slot(slot_id)`** - mark a slot `expired` once the agent
  releases its worktree. Called from `AgentSpawner._release_warm_pool_slot`
  (delegating to `release_warm_pool_slot` in `spawner_worktree.py`)
  during agent reaping.
- **`expire_stale(now=None)`** - sweep ready slots whose age is strictly
  greater than `slot_ttl_seconds` and move them to `expired`. Slots exactly
  at the TTL are not expired, and claimed or already expired slots are not
  changed. Nothing calls the sweep on a schedule yet.

For example, with `max_slots=2` and `slot_ttl_seconds=60`, add a `backend`
slot created at `100.0` and a `qa` slot created at `110.0`. Before a sweep,
`claim_slot("backend")` can still claim the first slot: claiming does not
compare its age with the TTL. Instead, explicitly sweep at the fixed clock
value `now=170.0`:

```python
pool.expire_stale(now=170.0)
```

The backend slot is 70 seconds old and expires; the qa slot is exactly 60
seconds old and stays ready because expiry uses `>` rather than `>=`. The
result is `pool.stats() == {"ready": 1, "claimed": 0, "expired": 1, "total": 2}`
and `pool.available_roles() == ["qa"]` (before the sweep, the available roles
were `["backend", "qa"]`). The expired backend record still occupies one of
the two lifetime slots: adding another slot is rejected at the cap. Expiry
changes the state and availability, not the total slot count.

The pool itself never **creates** slots - they are added externally; today
only `prepare_speculative_warm_pool` does so, with unprovisioned slots.
The pool just tracks state and serves claims FIFO. This
keeps `WarmPool` synchronous, lock-free, and easy to test (every state
transition produces a new immutable `PoolSlot`).

Pool stats are available via `pool.stats()` (`{ready, claimed, expired,
total}`) and `pool.available_roles()` for dashboards.

---

## When the pool is empty

Spawn proceeds on the cold path. There is no blocking, no retry, no
"please wait while we provision a slot". Two consequences:

- **Cold spawns are still safe.** A pool miss is just a missed
  optimization, not an error.
- **Bursts erode the pool fast.** Five steps in one stage with `roles:
  [backend]` and `max_slots: 3` will hit the pool twice and then cold-spawn
  three. Nothing refills the pool with real worktrees today.

Miss rates show in the logs (`logger.info("Using warm pool slot %s...")`
versus `worktree_mgr.create()` calls), but tuning does not help yet: the
`warm_pool:` section is not loaded, so `max_slots` and `warm_pool.roles`
from `bernstein.yaml` have no effect.

---

## Resource cost

Each ready slot is **a live git worktree** plus optionally **a live MCP
server process**. Concrete costs:

- **Disk**: roughly 1 worktree-worth per slot. Hardlinked under
  `.sdd/worktrees/`, so disk usage scales with how much your build
  produces, not the repo size itself.
- **Inodes**: significant on shallow filesystems with low inode budgets
  (CI runners). Watch this on Alpine-based images.
- **Memory / processes**: only if you start MCP servers per slot -
  otherwise zero RAM cost while idle.
- **Branch state**: each worktree owns a checked-out branch; you may
  see extra entries in `git worktree list`. Reaping stale slots prunes
  these.

Trade-off: slots cost real disk to save real seconds. Three slots is the
default sweet spot.

---

## When to disable

The pool is always constructed by the orchestrator and the `warm_pool:`
config block is not consulted yet, so there is no switch to turn it off.
If loading that block is wired in, skip it (or set `max_slots: 0`) when:

- **Every spawn already eats hours.** The 5–15 s saving is rounding
  error in long agentic runs.
- **You are debugging the spawner.** Determinism beats speed here -
  cold-path spawns are easier to reason about under a debugger.
- **CI / ephemeral envs.** On a fresh runner you'll never amortise the
  pool fill. Run cold.
- **Tight disk budget.** Each slot is a worktree; if you're already
  flirting with disk-full, deny yourself the optimisation.
- **Unusual roles.** If most spawns are for one-off roles not listed in
  `warm_pool.roles`, the pool will sit empty anyway. Either expand the
  role list or disable.


---

## Code pointers

| Concern | File |
|---------|------|
| Pool data structures + state machine | `src/bernstein/core/agents/warm_pool.py` |
| YAML config loader | `src/bernstein/core/agents/warm_pool.py` (`load_warm_pool_config`) |
| Spawner integration (claim) | `src/bernstein/core/agents/spawner_core.py` |
| Spawner integration (release) | `src/bernstein/core/agents/spawner_worktree.py` (`release_warm_pool_slot`) |
| Speculative slot preparation | `src/bernstein/core/tasks/task_lifecycle.py` (`prepare_speculative_warm_pool`) |
| Model-routing and tool-allowlist helpers on the spawn path | `src/bernstein/core/agents/spawner_warm_pool.py` |
| Merge / reap path | `src/bernstein/core/agents/spawner_merge.py` |

See also: [`state-persistence.md`](state-persistence.md) for the
`.sdd/worktrees/` layout each slot writes into;
[`adaptive-parallelism.md`](adaptive-parallelism.md) for the related
controller that decides how many agents to run at once (the warm pool
sizes the *latency* curve, adaptive parallelism sizes the *width*).
