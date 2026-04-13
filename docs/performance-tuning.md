# Performance Tuning Guide

This guide covers the key parameters that affect Bernstein's throughput, latency, and cost. Start with the quick-reference tables, then read the sections that apply to your workload.

All configurable constants (timeouts, thresholds, budget caps, tick intervals, etc.) are centralized in `src/bernstein/core/defaults.py`. Override them via the `tuning:` section in `bernstein.yaml`.

## Quick reference

| Workload size | Recommended `max_agents` | RAM needed | Typical cost/run |
|---|---|---|---|
| Small (< 50 files, 1–5 tasks) | 2–3 | 4 GB | $0.05–$0.50 |
| Medium (50–500 files, 5–30 tasks) | 4–6 | 8–16 GB | $0.50–$5 |
| Large (500+ files, 30+ tasks) | 8–16 | 32+ GB | $5–$50 |
| CI/CD pipeline | 1–2 | 2 GB | $0.10–$1 |
| Team shared server | 8–20 | 64+ GB | varies |

---

## `max_agents` for different API tiers

The right `max_agents` value depends on your provider's rate limits, not just your hardware. Exceeding provider rate limits causes 429 errors; Bernstein retries with exponential backoff, but throughput collapses.

### Claude (Anthropic)

| Subscription tier | Rate limit (approx.) | Recommended `max_agents` | Notes |
|---|---|---|---|
| **Free** | 50 req/day, ~5 req/min | 1 | Serial only; limited to experimentation |
| **Plus** | 1,000 req/day, ~50 req/min | 2–3 | Light workloads; expect throttling on bursts |
| **Pro** | 5,000 req/day, ~100 req/min | 4–6 | Default sweet spot; handles medium projects |
| **Enterprise / Tier 2+** | Custom SLA | 8–16 | Negotiate limits with your Anthropic account team |
| **Unlimited / Bedrock** | No hard cap | 16–32 | Limit by hardware and cost budget only |

> **Tip:** Check your actual quota with `bernstein status --provider` or via the Anthropic console. Bernstein reads `X-RateLimit-*` headers and backs off automatically, but it cannot predict limits — set `max_agents` below your burst ceiling.

### OpenAI / Gemini / Others

Apply the same principle: set `max_agents` so that peak parallelism stays below ~70% of your tier's requests-per-minute cap. Leave headroom for retries.

```yaml
# bernstein.yaml
max_agents: 6         # start here; tune up/down based on 429 rate
```

```bash
# Override at runtime
bernstein run --max-agents 10
# Or via environment variable
BERNSTEIN_MAX_AGENTS=10 bernstein run
```

---

## Concurrency vs. cost tradeoffs

Higher parallelism is not always cheaper. This section shows where the crossover points are.

### Throughput vs. spending

```
Tasks/hour        ▲
                  │     ●●●●● plateau (merge conflicts, rate limits)
                  │   ●●
                  │  ●
                  │●
                  └────────────────────────────► max_agents
                  1  2  3  4  6  8  12  20
```

- **1–4 agents**: Nearly linear throughput gains. Cost-per-task is dominated by model pricing.
- **4–8 agents**: Diminishing returns begin. Merge conflict rate rises; wasted work from conflicts adds cost.
- **8+ agents**: Merge conflicts, re-runs, and rate limiting can make total cost higher than a smaller fleet.

**Rule of thumb:** `max_agents = sqrt(task_count)` is a reasonable starting point for independent tasks. For tasks that share files, cut it in half.

### The idle-agent tax

An idle agent still holds a worktree on disk and occupies a slot. Watch `agent_idle_pct` in the dashboard:

```bash
bernstein status          # shows idle %
curl http://127.0.0.1:8052/status | jq '.metrics.agent_idle_pct'
```

If idle % stays above 40% for more than a few minutes, you have too many agents for the current backlog depth.

### Model selection per task complexity

Routing tasks to the cheapest model that can handle them cuts cost dramatically. Bernstein's bandit learns this automatically after ~5 observations per role, but you can configure defaults explicitly using `role_model_policy`:

```yaml
# bernstein.yaml
role_model_policy:
  docs:
    model: haiku        # $1/$5 per 1M tokens — documentation, formatting
    effort: low
  backend:
    model: sonnet       # $3/$15 per 1M tokens — feature implementation
    effort: high
  qa:
    model: sonnet
    effort: high
  architect:
    model: opus         # $5/$25 per 1M tokens — design, architecture review
    effort: max
  security:
    model: opus
    effort: max
```

**Estimated cost comparison** for a 10-task medium project (~100k tokens/task):

| Model | Input + Output cost | Relative cost | Best for |
|---|---|---|---|
| haiku | ~$0.60 | 1× (baseline) | docs, formatting, simple fixes |
| sonnet | ~$1.80 | 3× | feature implementation, tests |
| opus | ~$3.00 | 5× | architecture, security, design |

With the bandit optimizer (`EPSILON=0.1`, `QUALITY_THRESHOLD=0.80`), Bernstein converges on the cheapest model achieving ≥80% task success. The bandit state persists across runs in `.sdd/metrics/bandit_state.json`. Reset it to re-learn after a model upgrade:

```bash
rm .sdd/metrics/bandit_state.json
```

### `batch_size` and `tick_interval`

```yaml
batch_size: 3        # tasks dispatched per orchestrator tick
tick_interval: 3     # seconds between orchestrator cycles (default from defaults.py)
```

Default tick interval is 3 seconds (`ORCHESTRATOR.tick_interval_s` in `src/bernstein/core/defaults.py`).

- `batch_size ≤ max_agents / 2`: prevents a spike of unclaimable tasks when agents are busy.
- Low `tick_interval` (1–3s) improves responsiveness but adds CPU overhead from polling.
- High `tick_interval` (15–30s) is appropriate for slow-moving tasks (minutes each) or when CPU is constrained.

---

## Prompt caching optimization

Prompt caching lets Bernstein reuse provider-side KV-cache across agent turns. It reduces input token costs by 70–90% for repeated context.

### Cache pricing

| Model | Cache write | Cache read | Savings vs. full input |
|---|---|---|---|
| haiku | $1.25/M | $0.10/M | 90% on repeated reads |
| sonnet | $3.75/M | $0.30/M | 90% on repeated reads |
| opus | $6.25/M | $0.50/M | 90% on repeated reads |

Cache write costs slightly more than a regular input token. Break-even is at 2 reads; every additional read saves ~90%.

### What gets cached

Bernstein automatically caches:
- **System prompt** (role template + project context) — reused on every turn
- **File snapshots** injected into the prompt — reused if the file hasn't changed
- **Bulletin board contents** — shared findings across agents

### Configuration

```yaml
# bernstein.yaml
cache:
  enabled: true            # default: true
  min_tokens: 1024         # only cache blocks above this size
  ttl_minutes: 60          # cache lifetime on provider side (Claude: up to 5 min per block, extended with reuse)
```

### Maximizing cache hit rate

1. **Keep system prompts stable.** Every change to a role template busts the cache. Finalize prompts before long runs.
2. **Avoid dynamic timestamps in system prompts.** Injecting `datetime.now()` into the system prompt creates a unique prompt on every spawn — zero cache hits.
3. **Pass shared context as early context.** Files referenced in the first 1024+ tokens are cached; files appended late are not.
4. **Use `context_files` in `bernstein.yaml`** to preload stable reference files that all agents share:

```yaml
context_files:
  - README.md
  - docs/ARCHITECTURE.md
  - src/bernstein/core/models.py
```

### Reading cache metrics

```bash
bernstein cost --by model       # shows cost breakdown including cache savings
curl http://127.0.0.1:8052/status | jq '.metrics.cache_hit_rate'
```

Target: cache hit rate ≥ 50% in runs with 10+ agent turns per session.

---

## Worktree vs. branch isolation

Each agent gets an isolated git worktree by default. Understanding the tradeoffs helps you tune for speed vs. safety.

### Worktrees (default)

```
main branch
  └─ .sdd/worktrees/agent-abc123/   ← agent A
  └─ .sdd/worktrees/agent-def456/   ← agent B
  └─ .sdd/worktrees/agent-ghi789/   ← agent C
```

**Advantages:**
- True filesystem isolation — agents cannot step on each other's uncommitted changes.
- Merge happens only at task completion, not continuously.
- Supports sparse checkout for monorepos (only relevant paths checked out).

**Disadvantages:**
- Each worktree consumes disk space proportional to the working tree size.
- Symlinks (`.venv`, `node_modules`) reduce duplication but require Developer Mode on Windows.

```yaml
# bernstein.yaml — tune worktree setup
worktree_setup:
  symlink_dirs:
    - .venv
    - node_modules
  copy_files:
    - .env
  setup_command: null   # e.g., "npm install" or "uv sync"
```

**Disk estimate:** `worktree_size ≈ repo_size × (1 - symlink_ratio)`. For a 500 MB repo with `.venv` and `node_modules` symlinked, expect 50–100 MB per worktree.

### Branch-only isolation (no worktree)

Disabled by default. You can opt into a single-directory model where all agents share a checkout and coordinate via locks:

```yaml
worktree:
  enabled: false
```

Use this only when:
- Disk space is critically constrained.
- Tasks are strictly sequential (one at a time).
- You trust the agent not to corrupt shared state.

**Not recommended for `max_agents > 1`.**

### Cleaning up stale worktrees

After a crash or `SIGKILL`, orphaned worktrees accumulate. Clean them:

```bash
bernstein cleanup            # removes worktrees for completed/failed tasks
bernstein cleanup --force    # removes all non-active worktrees
```

Set automatic cleanup in config:

```yaml
janitor:
  worktree_cleanup_interval_s: 300   # check every 5 minutes
  max_orphan_age_s: 3600             # kill worktrees older than 1 hour
```

---

## Hardware requirements by workload size

### Minimal (solo dev, experimentation)

| Resource | Minimum | Recommended |
|---|---|---|
| RAM | 4 GB | 8 GB |
| CPU cores | 2 | 4 |
| Disk | 10 GB free | 20 GB free |
| Network | Any | Any |

Configuration: `max_agents: 2`, `model: haiku`

### Standard (team project, 5–30 tasks)

| Resource | Minimum | Recommended |
|---|---|---|
| RAM | 8 GB | 16 GB |
| CPU cores | 4 | 8 |
| Disk | 50 GB free | 100 GB SSD |
| Network | 100 Mbps | 1 Gbps |

Configuration: `max_agents: 4–6`, `model: sonnet`

### Large (monorepo, 30+ concurrent tasks)

| Resource | Minimum | Recommended |
|---|---|---|
| RAM | 32 GB | 64 GB |
| CPU cores | 8 | 16+ |
| Disk | 200 GB SSD | 500 GB NVMe |
| Network | 1 Gbps | 10 Gbps |

Configuration: `max_agents: 8–16`, mixed `sonnet`/`haiku`

### Per-agent breakdown

Each agent process uses:
- **RAM**: 200–500 MB (varies by CLI tool and context window size)
- **Disk**: 50–500 MB per worktree (depends on repo size and symlink config)
- **File descriptors**: ~50 per agent (git + stdin/stdout + file I/O)

```bash
# Increase file descriptor limits for high agent counts (Linux)
echo "* soft nofile 65536" | sudo tee -a /etc/security/limits.conf
echo "* hard nofile 65536" | sudo tee -a /etc/security/limits.conf

# macOS
sudo launchctl limit maxfiles 65536 200000
```

---

## Task queue tuning

### Priority scheduling

Lower priority number = dispatched first. Set priorities based on dependency chains:

```yaml
stages:
  - name: foundation
    steps:
      - title: "Set up database schema"
        priority: 1
  - name: features
    depends_on: [foundation]
    steps:
      - title: "Implement user API"
        priority: 3
```

### Task splitting

Large tasks slow throughput and increase context window costs. The orchestrator can split tasks automatically:

```yaml
task_splitting:
  max_files_per_task: 10
  max_estimated_tokens: 50000
```

Split at natural seams: one file, one test module, one API endpoint.

---

## System-level tuning

### Git performance

With many agents writing concurrently, git operations become a bottleneck:

```bash
git config core.fsmonitor true       # filesystem event monitor (macOS/Linux)
git config core.untrackedCache true  # cache untracked file state
git config pack.threads 4            # parallel pack operations
```

### WAL and disk I/O

Bernstein's write-ahead log (`wal`) writes synchronously by default for durability. On slow disks, this adds latency to every orchestrator tick.

Options:
- Store `.sdd/` on an SSD or NVMe drive.
- Mount `.sdd/runtime/wal/` on `tmpfs` for development (state lost on reboot):
  ```bash
  sudo mount -t tmpfs -o size=512m tmpfs /path/to/.sdd/runtime/wal
  ```
- Disable fsync (not recommended for production):
  ```yaml
  wal:
    fsync: false
  ```

### Memory limits

```yaml
memory:
  per_agent_limit_mb: 2048    # kill an agent if RSS exceeds this
  total_limit_mb: 16384       # pause spawning new agents above this system total
  oom_kill_enabled: true
```

---

## Monitoring and profiling

### Key metrics

```bash
bernstein status
curl http://127.0.0.1:8052/status | jq '.metrics'
```

| Metric | Healthy range | Action if outside |
|---|---|---|
| `tasks_completed_per_hour` | > 10 | Increase `max_agents` or reduce task size |
| `avg_task_duration_s` | < 300 | Check for stuck agents |
| `agent_idle_pct` | 10–30% | > 40%: fewer agents; < 5%: more agents |
| `merge_conflict_rate` | < 5% | Reduce `max_agents` or improve scope isolation |
| `cache_hit_rate` | > 50% | Fix dynamic system prompt content |
| `wal_write_latency_ms` | < 50 ms | Move WAL to faster disk |

### Prometheus

```yaml
prometheus:
  enabled: true
  port: 9090
```

Metrics at `http://localhost:9090/metrics` in Prometheus exposition format. Grafana dashboards are included in `deploy/grafana/`.

### CPU profiling

```bash
uv run python -m cProfile -o profile.out -m bernstein run
uv run python -c "import pstats; pstats.Stats('profile.out').sort_stats('cumulative').print_stats(20)"
```

### Debug bundle

Generate a comprehensive diagnostic archive:

```bash
bernstein debug    # collects logs, config, metrics, git state into a shareable bundle
```

Source: `src/bernstein/core/observability/debug_bundle.py`

### Running tests

Use the isolated test runner to avoid memory leaks:

```bash
uv run python scripts/run_tests.py -x
```

**Never** run `uv run pytest tests/` directly — it leaks 100+ GB RAM across 2000+ tests.

---

## Common bottlenecks

| Symptom | Likely cause | Fix |
|---|---|---|
| High merge conflict rate | Too many agents on overlapping files | Reduce `max_agents`; tighten `scope` in plan |
| Tasks queueing up | Not enough agents | Increase `max_agents` |
| High cost, low quality | Wrong model for task complexity | Configure `model_policy`; let bandit converge |
| Many 429 errors | Exceeding provider rate limit | Reduce `max_agents` to match API tier |
| High memory usage | Large context windows | Use smaller models; enable context compaction |
| Slow task dispatch | High `tick_interval` | Lower `tick_interval` to 2–5s |
| WAL write latency spikes | Slow disk | Move `.sdd/` to SSD or mount WAL on tmpfs |
| Stale worktrees filling disk | Orphaned agents after crash | Run `bernstein cleanup` or `git worktree prune` |
| Zero cache hits | Dynamic content in system prompt | Remove timestamps; fix `context_files` |
