# Warm Pool

**Why does the second agent spawn faster than the first?**

Spawning an agent is not free: Bernstein has to create a git worktree
(5–15 s on a non-trivial repo), optionally start an MCP server process,
and warm up the adapter's CLI. The **warm pool** pays that cost ahead of
time. A small number of "ready" slots - each backed by a pre-created
worktree and (optionally) a pre-started MCP process - sit idle until a
task arrives, at which point the spawner claims a slot instead of
provisioning from scratch.

If you only have time for one sentence: **set `warm_pool.max_slots: 3`
and the spawner will skip 5–15 s of cold-start on hot paths**. The rest
of this page explains lifecycle, sizing, and when to disable.

---

## What is a worktree slot?

A **worktree slot** is one pre-provisioned unit of the warm pool: a
ready-to-claim git worktree (plus, optionally, a pre-started MCP server
process) reserved for the next agent spawn. Each slot is a `PoolSlot`
object with its own `slot_id`, a target role, a `worktree_path` on
disk, and a `ready → claimed → expired` lifecycle. When a task
arrives, the spawner claims a ready slot and skips the 5–15 s
`git worktree add` cold-start; the pool then provisions a replacement
slot in the background. `warm_pool.max_slots` caps how many slots sit
idle at once.

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

When `Spawner._spawn_one()` needs a worktree it calls
`self._warm_pool.claim_slot(role)` first. On a hit, the pre-built
worktree path is reused and the slow `git worktree add` is skipped:

```python
warm_entry = self._warm_pool.claim_slot(role) if self._warm_pool is not None else None
if warm_entry is not None:
    spawn_cwd = Path(warm_entry.worktree_path)
    self._worktree_paths[session_id] = spawn_cwd
    self._worktree_roots[session_id] = worktree_repo_root
    self._warm_pool_entries[session_id] = warm_entry
    logger.info(
        "Using warm pool slot %s for session %s (role=%s)",
        warm_entry.slot_id, session_id, role,
    )
else:
    spawn_cwd = worktree_mgr.create(session_id)  # cold path
```

Source: `core/agents/spawner_core.py`.

On a miss, the spawner falls back to the cold path (`worktree_mgr.create`)
and the agent eats the 5–15 s - but the cold path remains the safe
default, so the warm pool only ever speeds things up.

Slot fill is **external** to the pool itself: a background task or the
spawner refills the pool by calling `pool.add_slot(slot)` after creating
a fresh worktree off the hot path. The pool just claims, releases, and
expires.

---

## Pool sizing

Configured under the `warm_pool:` section of `bernstein.yaml`:

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
| `max_slots` | `3` | Hard cap on simultaneously-living slots. The pool silently rejects `add_slot` calls that would exceed this (`warm_pool.py`). |
| `slot_ttl_seconds` | `300.0` | A `ready` slot older than this is moved to `expired` by `expire_stale()` so the worktree can be reaped (`warm_pool.py`). |
| `roles` | `[]` | Roles to pre-provision. The spawner only finds a hit if `claim_slot(role)` matches one of these. |

Source: `WarmPoolConfig` at `warm_pool.py`; loader at
`warm_pool.py`. Defaults are conservative - three slots covers
most projects without doubling your worktree footprint.

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
  Beyond `max_slots`, additions are silently ignored
  (`warm_pool.py`).
- **`claim_slot(role)`** - return the oldest `ready` slot matching
  `role`, marking it `claimed`. Returns `None` if no match exists; the
  spawner falls back to the cold path (`warm_pool.py`).
- **`release_slot(slot_id)`** - mark a slot `expired` once the agent
  releases its worktree. Called from `Spawner._release_warm_pool_slot`
  during agent reaping (`spawner_core.py`).
- **`expire_stale(now=None)`** - sweep ready slots older than
  `slot_ttl_seconds` and move them to `expired`. Run periodically by the
  spawner on its tick loop (`warm_pool.py`).

The pool itself never **creates** slots - that's the spawner / refiller
agent's job. The pool just tracks state and serves claims FIFO. This
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
  three. Refill happens in the background between ticks.

If you see consistent miss rates in logs (`logger.info("Using warm pool
slot %s...")` versus `worktree_mgr.create()` calls), bump `max_slots`
**and** add the missing roles to `warm_pool.roles`.

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

The pool is opt-in. Skip the `warm_pool:` config block (or set
`max_slots: 0`) when:

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

Re-enabling is just adding the config section back; no migration cost.

---

## Code pointers

| Concern | File |
|---------|------|
| Pool data structures + state machine | `src/bernstein/core/agents/warm_pool.py` |
| YAML config loader | `src/bernstein/core/agents/warm_pool.py` (`load_warm_pool_config`) |
| Spawner integration (claim) | `src/bernstein/core/agents/spawner_core.py` |
| Spawner integration (release) | `src/bernstein/core/agents/spawner_core.py` |
| Routing helper that picks model tier per slot | `src/bernstein/core/agents/spawner_warm_pool.py` |
| Merge / reap path | `src/bernstein/core/agents/spawner_merge.py...` |

See also: [`state-persistence.md`](state-persistence.md) for the
`.sdd/worktrees/` layout each slot writes into;
[`adaptive-parallelism.md`](adaptive-parallelism.md) for the related
controller that decides how many agents to run at once (the warm pool
sizes the *latency* curve, adaptive parallelism sizes the *width*).
