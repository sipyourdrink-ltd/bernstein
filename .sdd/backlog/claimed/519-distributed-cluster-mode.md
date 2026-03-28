# 519 — Distributed cluster mode: multi-instance Bernstein coordination

**Role:** architect
**Priority:** 1 (critical)
**Scope:** large

## Problem

Bernstein's task server binds to localhost:8052 with no auth. A single instance
can only orchestrate agents on one machine. Large teams or companies with many
Claude/Codex subscriptions cannot pool their resources into a unified workforce.

Ruflo/Claude Flow already ships distributed swarm topologies (hierarchical, mesh,
ring, star) with Raft consensus. Bernstein must match or exceed this.

## Design

### Phase 1: Remote task server
- Make `server_url` configurable via `bernstein.yaml` and env var `BERNSTEIN_SERVER_URL`
- Bind to `0.0.0.0` when `--remote` flag is passed (default stays `127.0.0.1`)
- Add bearer token auth (`BERNSTEIN_AUTH_TOKEN` env var) for all API endpoints
- TLS termination via reverse proxy (document nginx/caddy config)

### Phase 2: Multi-instance coordination
- Each Bernstein instance registers as a "node" with the central server
- Node heartbeat endpoint: `POST /nodes/{id}/heartbeat`
- Central server tracks live nodes, their capacity, active agents
- Task claiming uses optimistic locking (version field on task, CAS on claim)

### Phase 3: Cluster topologies
- **Star**: one central server, N worker nodes (simplest, default)
- **Mesh**: any node can serve tasks, gossip protocol for state sync
- **Hierarchical**: VP node coordinates cell-leader nodes, each with workers

### Phase 4: Auto-scaling
- Node can advertise available agent slots
- Central scheduler distributes tasks based on capacity + affinity
- Support heterogeneous nodes (some have GPU, some have Opus, etc.)

## Files to modify
- `src/bernstein/core/server.py` — bind address, auth middleware
- `src/bernstein/core/orchestrator.py` — configurable server_url
- `src/bernstein/core/bootstrap.py` — remote server support
- `src/bernstein/core/multi_cell.py` — cluster topology logic
- `bernstein.yaml` — cluster config section
- New: `src/bernstein/core/cluster.py` — node registration, heartbeat, topology

## Completion signal
- Two Bernstein instances on different machines coordinate on shared tasks
- `bernstein status` shows cluster view with all active nodes
- Integration test: start 2 servers, verify task handoff
