# Cluster Mode

Audience: SREs running Bernstein on more than one host.

## Overview

Cluster mode lets one Bernstein server coordinate work across many remote
worker nodes. Use it when one host can no longer absorb the agent workload,
when you need fault tolerance across machines, or when teams want a single
multi-tenant control plane in front of dedicated worker pools (GPU boxes,
region-pinned hosts).

Do *not* reach for cluster mode for single-host workloads. The single-process
orchestrator is simpler, faster to debug, and avoids the network failure modes
covered below. The architecture is intentionally a coordinator-with-workers
topology, not peer-to-peer; if you only have one machine the extra moving
parts are pure overhead.

## Architecture

There is exactly one **central server** (`bernstein run --remote` - the `run`
command is also exposed under the hidden alias `conduct`; both dispatch the
same function) and zero-or-more **worker nodes** (`bernstein worker`).
Workers never talk to each other. They register with the central server, send
heartbeats, claim tasks, and report completion. All scheduling decisions
happen on the central server.

Quick start:

```bash
# 1) start a central server reachable from workers
bernstein run --remote --goal "Build feature X"

# 2) start one or more workers
bernstein worker --server http://central-host:8052
```

```text
                    +--------------------+
                    |  Central server    |
                    |  (FastAPI+task DB) |
                    |                    |
                    | NodeRegistry       |
                    | TaskStealPolicy    |
                    | ClusterAuthen-     |
                    | ticator            |
                    +--------+-----------+
                             |
              +--------------+----------------+
              |              |                |
       +------v-----+  +-----v------+  +------v-----+
       | bernstein  |  | bernstein  |  | bernstein  |
       | worker A   |  | worker B   |  | worker C   |
       | (GPU box)  |  | (us-east)  |  | (laptop)   |
       +------------+  +------------+  +------------+
```

Code references:

- `src/bernstein/core/protocols/cluster/cluster.py:37` - `NodeRegistry`,
  the in-memory (optionally disk-persisted) registry of nodes.
- `src/bernstein/core/protocols/cluster/cluster.py:340` - `TaskStealPolicy`
  matches over- and under-loaded nodes.
- `src/bernstein/core/protocols/cluster/cluster.py:396` -
  `NodeHeartbeatClient` library used by `bernstein worker` to call back.
- `src/bernstein/core/routes/task_cluster.py:25` - every cluster HTTP
  endpoint listed below.
- `src/bernstein/core/protocols/cluster/cluster_auth.py:49` -
  `ClusterAuthenticator` (JWT issuance, verification, revocation).
- `src/bernstein/cli/commands/worker_cmd.py:50` - `WorkerLoop`, the
  worker-side run loop.
- `src/bernstein/core/fleet/` - fleet aggregator that can roll up the same
  cluster status across multiple Bernstein projects (see
  `core/fleet/aggregator.py`).

## Worker setup

A worker process is a thin loop that registers, heartbeats, claims tasks for a
configured set of roles, and spawns local CLI agents to execute them
(`worker_cmd.py:355`). Start one with:

```bash
bernstein worker --server http://central:8052 --token "$BERNSTEIN_AUTH_TOKEN"
```

Common flags (`worker_cmd.py:371-430`):

| Flag                       | Default            | Purpose                                                               |
| -------------------------- | ------------------ | --------------------------------------------------------------------- |
| `--server URL`             | required           | Central server URL. Also reads `BERNSTEIN_SERVER_URL`.                 |
| `--token TOKEN`            | from env           | Bearer token for cluster auth. Also reads `BERNSTEIN_AUTH_TOKEN`.      |
| `--name NAME`              | hostname           | Worker name shown in `/cluster/nodes`.                                 |
| `--slots N`                | 6                  | Maximum concurrent agents on this worker (= node capacity).            |
| `--roles a,b,c`            | backend,qa,security,frontend | Roles this worker accepts.                                |
| `--label k=v`              | (repeat)           | Free-form labels for affinity routing.                                 |
| `--adapter NAME`           | auto-detect        | Which CLI agent to invoke (`claude`, `codex`, ...).                    |
| `--model NAME`             | adapter default    | Default model for tasks with no explicit `model`. Non-Claude adapters fall back to their own discovered default. |
| `--poll-interval SECONDS`  | 10                 | Task poll cadence.                                                     |
| `--poll-interval-ms MS`    | none               | Override poll cadence in milliseconds.                                 |
| `--heartbeat-interval-ms`  | 15000              | Heartbeat cadence.                                                     |

Capacity is set with `--slots`. Internally the worker tracks `available_slots
= max_agents - len(active_tasks)` and reports it on every heartbeat
(`worker_cmd.py:104-108`). Pick a value that reflects how many parallel
adapter processes the host can comfortably run. There is no auto-detection -
oversubscribing a worker just queues longer.

Worker environment requirements:

- A logged-in CLI agent (`claude`, `codex`, `gemini`, `qwen`, `aider`).
  Auto-detection uses `agent_discovery.discover_agents_cached()`
  (`worker_cmd.py:38-46`).
- Python 3.12+ runtime with the same Bernstein version as the central server.
- Network reachability to the central server's HTTP port.
- A writable `--workdir` (defaults to `cwd`) for task worktrees.

## JWT node authentication

Cluster auth is JWT-based and is the recommended deployment mode in
production. The flow is:

1. **Server config** - pass a `ClusterAuthConfig(secret=..., require_auth=True)`
   into `ClusterAuthenticator` and mount it on
   `app.state.cluster_authenticator` (`cluster_auth.py:32-46`,
   `task_cluster.py:54`). When `require_auth=False`, the verifier returns a
   synthetic anonymous payload (`cluster_auth.py:113-121`) - useful for tests
   only.
2. **Token issuance** - the server issues a node token via
   `ClusterAuthenticator.issue_node_token(node_id)`
   (`cluster_auth.py:70-93`). By default the token carries
   `node:register` and `node:heartbeat` scopes; admin tokens additionally
   require `node:admin`.
3. **Worker presents token** - the worker sends `Authorization: Bearer
   <token>` on every cluster HTTP call (`worker_cmd.py:98-102`).
4. **Per-request verification** - `task_cluster._verify_cluster_auth()` resolves
   the required scope per route (`SCOPE_NODE_REGISTER`,
   `SCOPE_NODE_HEARTBEAT`, `SCOPE_NODE_ADMIN`) and rejects with HTTP 401 on
   any failure (`task_cluster.py:44-60`).
5. **Heartbeat-bound identity** - the heartbeat verifier checks that the
   token's `user_id` matches the path's `node_id`
   (`cluster_auth.py:262-265`); a stolen heartbeat token cannot be replayed
   against a different node.
6. **Revocation** - `revoke_token(token)` and `revoke_node(node_id)` mark a
   token unusable for subsequent verifications (`cluster_auth.py:174-191`).
   Token revocation is in-memory; for persistence across restarts use
   short-lived tokens (default 24h, `ClusterAuthConfig.token_expiry_hours`).

Default scopes, defined in `cluster_auth.py:23-25`:

| Scope             | Required by                                         |
| ----------------- | --------------------------------------------------- |
| `node:register`   | `POST /cluster/nodes`                               |
| `node:heartbeat`  | `POST /cluster/nodes/{id}/heartbeat`                |
| `node:admin`      | `cordon`, `uncordon`, `drain`, `DELETE /cluster/nodes` |

The worker side of the flow lives in `worker_cmd.py:134-164` (registration)
and `:165-190` (heartbeat). On HTTP 404 from the heartbeat the worker
re-registers automatically (eviction recovery).

## Operational primitives: drain / cordon / uncordon / steal

Every primitive is one HTTP POST. The server-side handlers all live in
`task_cluster.py` and ultimately call into `NodeRegistry`
(`cluster.py:155-185`).

| Operation                       | Endpoint                                        | What it does                                                            |
| ------------------------------- | ----------------------------------------------- | ----------------------------------------------------------------------- |
| **Cordon a node**               | `POST /cluster/nodes/{id}/cordon`               | Sets status to `CORDONED`. Node still heartbeats but is excluded from scheduling (`cluster.py:155-163`). |
| **Uncordon a node**             | `POST /cluster/nodes/{id}/uncordon`             | Restores `ONLINE` status from `CORDONED` or `DRAINING` (`cluster.py:165-174`). |
| **Drain a node**                | `POST /cluster/nodes/{id}/drain`                | Sets status to `DRAINING`. Equivalent to cordon + signal - agents finish their current tasks but no new work is assigned (`cluster.py:176-184`). |
| **Trigger task stealing**       | `POST /cluster/steal`                           | Server runs `TaskStealPolicy.find_steal_pairs()` and resets eligible tasks back to `open` so quieter nodes can claim them (`task_cluster.py:197-246`, `cluster.py:340-393`). |
| **Unregister (graceful exit)**  | `DELETE /cluster/nodes/{id}`                    | Removes the node from the registry. Workers call this on `SIGINT` / `SIGTERM` (`worker_cmd.py:310-322`). |

Task stealing thresholds (`cluster.py:351-358`):

- `overload_threshold=5` - a node with more than 5 queued tasks is a *donor*.
- `idle_threshold=2` - a node with at least 2 free slots is a *receiver*.
- `max_steal_per_tick=3` - at most 3 tasks move per call.

The body of `POST /cluster/steal` is `{"queue_depths": {"node-a": 7,
"node-b": 0, ...}}`; the response lists `(donor_node_id, receiver_node_id,
task_ids)` actions and a total count (`task_cluster.py:213-246`).

Operationally, drain is the right primitive for a planned restart: cordon →
drain → wait for `active_agents = 0` → unregister. Cordon is the right
primitive for an unhealthy host you want to investigate without rebooting.

## Failure modes

Bernstein's cluster recovery is conservative - there is no global lock
service, no leader election, no consensus protocol. Behaviours below are
exact descriptions of what the code does today.

**A worker disappears (network partition, crash, host reboot).** The central
server keeps the node entry. Each tick the orchestrator (or any caller of
`NodeRegistry.mark_stale()`, `cluster.py:197-210`) checks
`last_heartbeat`; if it exceeds `node_timeout_s` the node is flipped to
`OFFLINE`. The node's claimed tasks are *not* automatically reassigned -
they remain in `claimed` status. Operators recover them with
`POST /cluster/steal` (donor=offline-node) or by deleting and re-claiming
the tasks. When the worker returns, its first heartbeat re-registers it
(`cluster.py:185-195`).

**Partial network partition (worker can heartbeat but not claim tasks).**
The worker's `_claim_task()` tolerates HTTP errors silently
(`worker_cmd.py:202-204`); the heartbeat path is independent. The node
appears healthy in `/cluster/status` but does no useful work. Detection:
watch for nodes with `available_slots == max_agents` despite
`active_agents == 0` and a non-zero pending task count.

**Server restart with active workers.** `NodeRegistry` optionally persists
to a JSON file (`persist_path` in `cluster.py:50-81`). On startup all
loaded nodes are marked `OFFLINE` until they heartbeat
(`cluster.py:72-76`); fresh tokens are still valid (JWT secret survives the
restart). Workers detect eviction via HTTP 404 on heartbeat and re-register
via `_register_with_retry()` (`worker_cmd.py:260-270`).

**Worker holds a stale token after revocation.** The next mutating call
returns 401. The worker does *not* automatically renew - issue a fresh
token externally and pass it via `BERNSTEIN_AUTH_TOKEN`. Heartbeats are
separately scoped, so a heartbeat-only token cannot be promoted to
`node:admin` operations.

**Worker's CLI adapter is missing or logged out.** Auto-detection falls
back to `"claude"` (`worker_cmd.py:46-47`). The `_spawn_agent()` call will
raise inside `AgentSpawner.spawn_for_task()` and the worker logs a warning
without crashing (`worker_cmd.py:230-258`); the task is left unclaimed.

## Observability for cluster health

Endpoints relevant to cluster operations:

- `GET /cluster/nodes[?status=online|cordoned|draining|offline]` - full
  node list (`task_cluster.py:163-177`).
- `GET /cluster/status` - aggregate summary (`task_cluster.py:180-194`):
  topology, total/online/offline node counts, total capacity, available
  slots, active agents, full node list.
- Per-node fields surfaced: `last_heartbeat`, `registered_at`,
  `capacity.{max_agents,available_slots,active_agents,gpu_available,supported_models}`,
  `labels`, `cell_ids` (`cluster.py:274-292`).

Pair these with the standard observability surface
(`/metrics`, `/grafana/dashboard`, `/slo`) - see
[Observability overview](observability-overview.md). The fleet aggregator
in `core/fleet/aggregator.py` is the right primitive when you have multiple
Bernstein projects you want rolled into a single dashboard; it scrapes each
project's `/cluster/status` and `/status` endpoints.

For node JWT issuance and revocation, audit events flow through the
standard audit log (`core/security/audit.py`); see
[Security and identity](security-and-identity.md) for the integrity
guarantees and how to export them.

## Code pointers

| Concern                          | File                                                                            |
| -------------------------------- | ------------------------------------------------------------------------------- |
| Cluster routes                   | `src/bernstein/core/routes/task_cluster.py`                                     |
| NodeRegistry, persistence        | `src/bernstein/core/protocols/cluster/cluster.py`                               |
| JWT cluster auth                 | `src/bernstein/core/protocols/cluster/cluster_auth.py`                          |
| Task-stealing policy             | `src/bernstein/core/protocols/cluster/cluster.py:340-393`                       |
| Worker CLI                       | `src/bernstein/cli/commands/worker_cmd.py`                                      |
| Worker run loop                  | `src/bernstein/cli/commands/worker_cmd.py:50-368`                               |
| Heartbeat client (library)       | `src/bernstein/core/protocols/cluster/cluster.py:396-...`                       |
| Cluster autoscaler (optional)    | `src/bernstein/core/protocols/cluster/cluster_autoscaler.py`                    |
| Fleet aggregator                 | `src/bernstein/core/fleet/aggregator.py`                                        |
| Models / data classes            | `src/bernstein/core/models.py` - `NodeInfo`, `NodeCapacity`, `NodeStatus`, `ClusterConfig` |
