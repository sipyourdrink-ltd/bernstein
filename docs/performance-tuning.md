# Performance Tuning Guide

This guide covers the key parameters that affect Bernstein's throughput, latency, and resource consumption.

## Key parameters

### max_agents

Maximum number of concurrent CLI agents. This is the primary throughput knob.

```yaml
# bernstein.yaml
max_agents: 6
```

| Workload | Recommended | Notes |
|----------|-------------|-------|
| Small project (< 50 files) | 2-3 | Reduces merge conflicts |
| Medium project (50-500 files) | 4-6 | Default setting |
| Large monorepo (500+ files) | 8-12 | Needs 32+ GB RAM |
| CI/CD pipeline | 1-2 | Serial execution for predictability |

Each agent consumes 200-500 MB of resident memory depending on the backing CLI tool and context window size. Scale accordingly.

### batch_size

Number of tasks dispatched per orchestrator tick.

```yaml
batch_size: 3
```

- Higher values improve throughput when tasks are independent.
- Lower values reduce wasted work when tasks have implicit dependencies.
- Rule of thumb: `batch_size <= max_agents / 2` avoids idle waiting.

### tick_interval

Seconds between orchestrator decision cycles.

```yaml
tick_interval: 5
```

- Lower values (1-3s): more responsive, higher CPU usage from polling.
- Higher values (10-30s): less responsive, lower overhead. Appropriate for slow-moving tasks that run for minutes.
- Default (5s) is a good balance for most workloads.

### Memory management

Bernstein tracks per-agent memory usage and can terminate agents that exceed limits.

```yaml
memory:
  per_agent_limit_mb: 2048
  total_limit_mb: 16384
  oom_kill_enabled: true
```

#### Context window sizing

Agents with large context windows consume more provider API tokens. Tune the model selection to match task complexity:

```yaml
model_policy:
  simple: "haiku"      # Low complexity tasks
  medium: "sonnet"     # Standard tasks
  complex: "opus"      # Architecture, design decisions
```

## Task queue tuning

### Priority scheduling

Tasks are dispatched by priority (lower number = higher priority). Set priority based on dependency chains:

```yaml
stages:
  - name: foundation
    steps:
      - goal: "Set up database schema"
        priority: 1
  - name: features
    depends_on: [foundation]
    steps:
      - goal: "Implement user API"
        priority: 3
```

### Task splitting

Large tasks should be split into smaller units. The orchestrator's built-in splitter can decompose tasks:

```yaml
task_splitting:
  max_files_per_task: 10
  max_estimated_tokens: 50000
```

## System-level tuning

### File descriptors

Each agent process needs file descriptors for stdin/stdout/stderr, git operations, and file I/O. Increase limits for high agent counts:

```bash
# /etc/security/limits.conf (Linux)
* soft nofile 65536
* hard nofile 65536

# macOS
sudo launchctl limit maxfiles 65536 200000
```

### Git performance

With many agents writing concurrently, git operations become a bottleneck.

```bash
# Enable filesystem monitor for large repos
git config core.fsmonitor true
git config core.untrackedCache true

# Increase pack threads
git config pack.threads 4
```

### Disk I/O

The WAL (Write-Ahead Log) writes synchronously with fsync for durability. On slow disks this adds latency to every orchestrator decision.

Options:
- Use an SSD for `.sdd/` directory
- Mount `.sdd/runtime/wal/` on tmpfs if durability is not critical (dev environments)
- Set `wal.fsync: false` in config (not recommended for production)

## Monitoring performance

### Built-in metrics

```bash
# Check current status
bernstein status

# View task throughput
curl http://127.0.0.1:8052/status | jq '.metrics'
```

Key metrics to watch:
- `tasks_completed_per_hour`: overall throughput
- `avg_task_duration_s`: latency per task
- `agent_idle_pct`: capacity utilization (high = over-provisioned)
- `merge_conflict_rate`: contention indicator
- `wal_write_latency_ms`: disk I/O health

### Prometheus export

Enable the Prometheus exporter for time-series monitoring:

```yaml
prometheus:
  enabled: true
  port: 9090
```

Metrics are exposed at `http://localhost:9090/metrics` in Prometheus exposition format.

## Profiling

### CPU profiling

```bash
# Profile the orchestrator
uv run python -m cProfile -o profile.out -m bernstein run
# Analyze
uv run python -c "import pstats; pstats.Stats('profile.out').sort_stats('cumulative').print_stats(20)"
```

### Memory profiling

```bash
# Track memory over time
uv run python -m tracemalloc -m bernstein run
```

## Common bottlenecks

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| High merge conflict rate | Too many agents on overlapping files | Reduce `max_agents` or improve scope isolation |
| Tasks queueing up | Not enough agents | Increase `max_agents` |
| High memory usage | Large context windows | Use smaller models for simple tasks |
| Slow task dispatch | High `tick_interval` | Reduce `tick_interval` |
| WAL write latency spikes | Slow disk | Move WAL to SSD or tmpfs |
| Agent spawn latency | CLI tool startup time | Use agent pooling (if supported) |
