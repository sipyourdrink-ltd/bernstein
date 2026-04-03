# Cluster Mode

Bernstein can run in a distributed topology where one server coordinates work and remote workers execute tasks.

This is an advanced deployment mode: implemented and usable, but not positioned as a fully managed cluster product.

---

## Architecture

```text
Central Bernstein server
  + cluster routes in task API
    + remote worker nodes (bernstein worker)
```

Core implementation paths:

- `src/bernstein/cli/worker_cmd.py`
- `src/bernstein/core/cluster.py`
- `src/bernstein/core/routes/tasks.py` (cluster endpoints)

---

## Quick start

### 1) Start a central server reachable from workers

```bash
bernstein conduct --remote --goal "Build feature X"
```

The `conduct` command (alias for `run`) supports `--remote` which binds the server to `0.0.0.0` for cluster access. The `--remote` flag is not available on the standard `bernstein run` command.

### 2) Start workers

```bash
bernstein worker --server http://central-host:8052
```

Useful options include `--name`, `--slots`, `--roles`, `--label`, `--token`.

### 3) Inspect cluster status

```bash
curl http://central-host:8052/cluster/status
curl http://central-host:8052/cluster/nodes
```

---

## Cluster API endpoints

Implemented endpoints:

- `POST /cluster/nodes`
- `POST /cluster/nodes/{node_id}/heartbeat`
- `DELETE /cluster/nodes/{node_id}`
- `GET /cluster/nodes`
- `GET /cluster/status`
- `POST /cluster/steal`

The task-steal endpoint executes best-effort redistribution logic and resets selected tasks back to claimable state.

---

## Authentication and environment

Common environment variables:

- `BERNSTEIN_SERVER_URL` (worker default server URL)
- `BERNSTEIN_AUTH_TOKEN` (shared auth token where enabled)

Example:

```bash
export BERNSTEIN_SERVER_URL=http://central-host:8052
export BERNSTEIN_AUTH_TOKEN=replace-me
bernstein worker
```

---

## Operational caveats

- Cluster behavior depends on network reliability and worker health reporting.
- For high concurrency and strict consistency requirements, validate storage/locking topology explicitly.
- Treat label-based affinity and task stealing as optimization primitives, not hard scheduling guarantees.

---

## Recommended use

Use cluster mode when:

- a single host becomes a bottleneck,
- you can operate multiple worker machines reliably,
- and you can monitor API + worker health continuously.

For most teams, single-host orchestration remains the default starting point.
