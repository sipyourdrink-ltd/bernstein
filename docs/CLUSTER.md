# Distributed Cluster Mode

Run Bernstein across multiple machines. A **central server** coordinates
tasks while **worker nodes** pull work, spawn agents, and report results.

## Architecture

```
                    +-----------------+
                    | Central Server  |
                    | (bernstein run  |
                    |  --remote)      |
                    +--------+--------+
                             |
              +--------------+--------------+
              |              |              |
        +-----+----+  +-----+----+  +-----+----+
        | Worker 1  |  | Worker 2  |  | Worker 3  |
        | (worker)  |  | (worker)  |  | (worker)  |
        +-----------+  +-----------+  +-----------+
```

**Topology:** Star (default). One central server, N workers.
Workers register via HTTP, send heartbeats every 15s, and pull tasks
using the same `/tasks/next/{role}` API that local agents use.

## Quick Start

### 1. Start the central server

On the machine that will coordinate work:

```bash
bernstein run --remote --goal "Build feature X"
```

`--remote` binds the server to `0.0.0.0:8052` (default is `127.0.0.1`).

### 2. Join worker nodes

On each worker machine:

```bash
bernstein worker --server http://central-host:8052
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--server` | (required) | Central server URL |
| `--name` | hostname | Worker node name |
| `--slots` | 6 | Max concurrent agents |
| `--roles` | backend,qa,security,frontend | Accepted task roles |
| `--label` | (none) | Node labels (key=value, repeatable) |
| `--token` | (none) | Bearer token for auth |
| `--adapter` | auto-detect | CLI agent (claude/codex/gemini/qwen) |
| `--poll-interval` | 10 | Seconds between task polls |

### 3. Monitor the cluster

```bash
# From any machine with network access
curl http://central-host:8052/cluster/status | jq
```

Output:
```json
{
  "topology": "star",
  "total_nodes": 3,
  "online_nodes": 3,
  "offline_nodes": 0,
  "total_capacity": 18,
  "available_slots": 12,
  "active_agents": 6
}
```

## Authentication

Set a shared token on both server and workers:

```bash
# Central server
export BERNSTEIN_AUTH_TOKEN=secret-token-here
bernstein run --remote

# Workers
bernstein worker --server http://central:8052 --token secret-token-here
```

Or use the environment variable on workers too:

```bash
export BERNSTEIN_AUTH_TOKEN=secret-token-here
export BERNSTEIN_SERVER_URL=http://central:8052
bernstein worker
```

## Task Stealing (Load Balancing)

When one worker has more queued tasks than it can handle, the central
server can rebalance by "stealing" tasks and making them available to
idle workers.

The steal policy runs when workers report queue depths via the
`POST /cluster/steal` endpoint:

- **Overload threshold:** A node with >5 queued tasks is considered
  overloaded (configurable).
- **Idle threshold:** A node with >=2 free slots is a candidate to
  receive stolen tasks.
- **Max steal per tick:** Up to 3 tasks are moved per rebalancing cycle.

Stolen tasks are reset to `open` status so they can be claimed by any
worker.

## Distributed Locking (Redis)

For high-contention workloads, enable Redis-based distributed locks to
prevent two workers from claiming the same task:

```bash
pip install bernstein[cluster]  # installs redis
export BERNSTEIN_REDIS_URL=redis://redis-host:6379/0
```

The `RedisCoordinator` uses a single-node Redlock approach (`SET NX PX`)
with Lua-script-guarded release. Without Redis, the task server's
built-in asyncio lock serializes claims (sufficient for most deployments).

## Cluster API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/cluster/nodes` | POST | Register a new worker node |
| `/cluster/nodes/{id}/heartbeat` | POST | Send heartbeat + update capacity |
| `/cluster/nodes/{id}` | DELETE | Unregister a node |
| `/cluster/nodes` | GET | List all nodes (filter by ?status=) |
| `/cluster/status` | GET | Cluster summary |
| `/cluster/steal` | POST | Trigger task stealing rebalance |

## Worker Lifecycle

1. **Register:** Worker sends `POST /cluster/nodes` with its name,
   capacity, and labels.
2. **Heartbeat:** Every 15s, sends capacity update. If the central
   server doesn't hear from a node for 60s, it marks it offline.
3. **Claim:** Worker polls `GET /tasks/next/{role}` for each of its
   configured roles. Claims are atomic (version-based CAS prevents
   double-claiming).
4. **Execute:** Worker spawns a CLI agent process (Claude Code, Codex,
   etc.) for each claimed task.
5. **Report:** On completion, worker sends `POST /tasks/{id}/complete`.
   On failure, `POST /tasks/{id}/fail`.
6. **Shutdown:** Worker sends `DELETE /cluster/nodes/{id}` to cleanly
   unregister.

## Node Labels and Affinity

Use labels to direct specific tasks to specialized workers:

```bash
# GPU worker
bernstein worker --server http://central:8052 --label gpu=true --label region=us-east

# The central server's best_node_for_task() prefers nodes with matching labels
```

## Failure Handling

- **Worker crash:** Central server marks the node offline after 60s
  without heartbeat. Claimed tasks can be force-reclaimed by other
  workers.
- **Central server restart:** Workers re-register automatically when
  their heartbeat returns a 404.
- **Network partition:** Workers retry registration every 5s. Tasks
  claimed during partition are protected by version-based CAS — no
  double-execution.

## Example: 3-Machine Cluster

```bash
# Machine A (central)
bernstein run --remote --goal "Build auth system" --budget 20

# Machine B (GPU worker)
bernstein worker --server http://machine-a:8052 \
  --name gpu-worker --slots 4 --label gpu=true

# Machine C (CPU worker)
bernstein worker --server http://machine-a:8052 \
  --name cpu-worker --slots 8 --roles backend,qa
```
