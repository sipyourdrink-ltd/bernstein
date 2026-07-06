# Cluster 2-node end-to-end harness

The single-process cluster tests prove the algorithm. The 2-node
harness proves the **system** - real HTTP between two interpreters,
real process kills, real network partitions, real token expiry. Six
chaos scenarios run in CI on a dedicated workflow.

This page is for contributors and operators evaluating cluster mode
production-readiness. End-users do not interact with the harness.

## Why it exists

Customers asking "is cluster mode production-ready?" want to see
tests that run two real processes and survive at least:

- One node dies mid-operation
- Network drops packets between heartbeats
- Token expires mid-task-steal
- Central server restarts and reloads node registry from JSON

Mocking the HTTP layer in a single Python process cannot prove any
of that.

## How it works

Fixtures live in `tests/integration/cluster/conftest.py`. The
`two_node_cluster` fixture boots two real processes via
`subprocess.Popen`, listens on OS-allocated ephemeral ports, and
yields a `ClusterHandle`:

```python
def test_worker_crash_mid_task(two_node_cluster):
    handle = two_node_cluster
    task_id = handle.submit_task(role="backend", goal="...")
    handle.kill_worker()                         # SIGKILL
    handle.wait_for_node_status("offline", timeout=15)
    handle.restart_worker()
    handle.wait_for_task_completion(task_id, timeout=60)
    assert handle.task_state(task_id) == "completed"
```

Helper methods on `ClusterHandle`:

- `kill_worker()` / `restart_worker()` - process control
- `restart_central()` - central server restart from the JSON registry
- `block_traffic_for(seconds)` - Linux uses `iptables`; macOS uses a
  Python proxy
- `current_status()` - snapshot of the registry

## The six chaos scenarios

| Test | What it asserts |
|---|---|
| Happy path | register → heartbeat → task → complete; state matches on both sides. |
| Worker crash mid-task | Central marks worker OFFLINE within heartbeat timeout; task is re-claimable. |
| Central restart | Worker re-registers cleanly on next heartbeat after central reboots. |
| Network partition | Worker goes OFFLINE during partition, reconciles on restoration. |
| Token expiry mid-flight | 5 s JWT; sleep 6 s; heartbeat rejected; refresh succeeds. |
| Concurrent claims | Two workers race for the same task; exactly one succeeds. |

## How to run them

These tests are **skipped by default** (`@pytest.mark.cluster_e2e`,
`@pytest.mark.slow`). Run locally:

```bash
# Linux (full coverage including iptables-based partition test)
uv run pytest tests/integration/cluster/test_real_2node.py \
    -m cluster_e2e -x -q

# macOS (skips iptables tests; uses Python proxy)
PARTITION_BACKEND=python_proxy \
    uv run pytest tests/integration/cluster/test_real_2node.py \
    -m cluster_e2e -x -q
```

In CI, `.github/workflows/cluster-e2e.yml` runs them on PRs touching
`core/protocols/cluster/**` and on a nightly schedule.

When the mTLS fixture is parameterised (`tls=on`), the same six
scenarios run with TLS enabled - no test rewrites required.

## Diagnostics on failure

When a scenario fails, the harness collects:

- Process logs from both nodes
- Last 100 audit-log lines
- The `current_status()` registry snapshot

These are written to `tests/_artifacts/cluster_e2e/<test_name>/`.

## Limitations

- Linux + macOS only (no Windows in CI).
- Targets STAR topology only.
- No autoscaler chaos.
- Throughput / load testing (>2 nodes, hundreds of tasks) is out of
  scope; this harness focuses on correctness under failure.
- iptables tests require root or a privileged Docker container.

## Related

- Harness: `tests/integration/cluster/conftest.py`
- Tests: `tests/integration/cluster/test_real_2node.py`
- CI: `.github/workflows/cluster-e2e.yml`
- [Cluster mTLS setup](../cluster/mtls-setup.md)
- [Cluster observability](../observability/cluster.md)
