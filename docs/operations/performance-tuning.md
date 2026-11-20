# Performance Tuning Guide

This guide covers the key parameters that affect Bernstein's throughput, latency, and cost. Start with the quick-reference tables, then read the sections that apply to your workload.

All configurable constants (timeouts, thresholds, budget caps, tick intervals, etc.) are centralized in `src/bernstein/core/defaults.py`. Override them via the `tuning:` section in `bernstein.yaml`.

## Measure CLI startup

Time the installed `bernstein --help` and `bernstein config-path --json`
in fresh processes with an empty workspace and isolated user configuration.
Use the same lockfile, Python version and fixture for both revisions.
Measure the installed console script directly so dependency setup stays
outside the timer. For a cold-bytecode diagnostic, give each process a
fresh `PYTHONPYCACHEPREFIX`. For warm runs, prime and reuse that directory.
The external-process harness records wall time, exit codes and output hashes.

```bash
PATH=/usr/bin:/bin .venv/bin/python benchmarks/bench_cli_startup.py \
  --console .venv/bin/bernstein --runs 5 --importtime-dir /tmp/bernstein-imports
```

Use `--entrypoint module` for the installed Python module path and `--runs 50`
to include a p95. Use `--cache warm` or `--cache cold` to select one mode.
Import traces are collected separately with empty bytecode directories.
Cold samples preserve the OS filesystem cache. Root help reads declared
plugin names. Selecting an adapter validates the implementation.

For a local run, the harness starts a new Git project with an empty app home
and the idle adapter. It polls the real readiness endpoint and fetches
dashboard data on the run's loopback port. It records the first agent
registration, the first working or idle agent, and the CLI exit separately. It cleans up the processes it started.

```bash
.venv/bin/python benchmarks/bench_local_readiness.py \
  --console .venv/bin/bernstein --runs 50 --output readiness.json
```

On POSIX, measure actual terminal feedback separately. The ordinary run
fixture sends Ctrl-C after its first preparation message and records the
real interruption exit code. Omitting `--execute` times a completed plan
preview. Pass `--warm-home` to either harness to prime and reuse user caches.

```bash
.venv/bin/python benchmarks/bench_terminal_feedback.py --execute \
  --console .venv/bin/bernstein --runs 50 --output terminal.json
```

Use an installed wheel and report first-launch provisioning separately for
npm, pipx or uv. The first call can need to download and install dependencies.
The installed reference budgets are 250 ms for a meaningful terminal frame,
500 ms for help or version to finish, and 2 s for a usable local dashboard.

The spawner refreshes the optional code index in the background. A run
uses the installed specialist catalog. To update it for a future run, use
`bernstein agents sync`. A catalog path specified in the seed must exist.

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

Rate limits vary by plan and change over time; consult your provider console (for Anthropic, the usage and limits pages) for your actual quota rather than any published table. As a rule of thumb: start at `max_agents: 1` on trial or entry-level tiers, 4-6 on a standard paid API tier, and 8+ only on enterprise or other high-quota hosted deployments where the cap is hardware and cost budget.

> **Tip:** `bernstein status` shows a provider/quota table once the orchestrator has recorded provider snapshots. Bernstein reads `X-RateLimit-*` headers and backs off automatically, but it cannot predict limits - set `max_agents` below your burst ceiling.

### OpenAI / Gemini / Others

Apply the same principle: set `max_agents` so that peak parallelism stays below ~70% of your tier's requests-per-minute cap. Leave headroom for retries.

```yaml
# bernstein.yaml
max_agents: 6         # start here; tune up/down based on 429 rate
```

Set `max_agents` in the run's `bernstein.yaml` before starting
Bernstein. The orchestrator reads the limit from that seed.

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

An idle agent still holds a worktree on disk and occupies a slot. Inspect
the per-agent states and task assignments in the status response:

```bash
bernstein status
curl http://127.0.0.1:8052/status | jq '.agents.items[] | {id, status, task_ids}'
```

The response exposes an `agents` object with `count` and `items`; each item
includes its `status` and assigned `task_ids`. If agents remain `idle`
across repeated snapshots while the backlog is shallow, consider reducing
`max_agents`. The response does not expose an idle percentage.

### Model selection per task complexity

Routing tasks to the cheapest model that can handle them cuts cost dramatically. Bernstein's bandit learns this automatically after ~5 observations per role, but you can configure defaults explicitly using `role_model_policy`:

```yaml
# bernstein.yaml
role_model_policy:
  docs:
    model: haiku        # $1/$5 per 1M tokens - documentation, formatting
    effort: low
  backend:
    model: sonnet       # $3/$15 per 1M tokens - feature implementation
    effort: high
  qa:
    model: sonnet
    effort: high
  architect:
    model: opus         # $5/$25 per 1M tokens - design, architecture review
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
- **System prompt** (role template + project context) - reused on every turn
- **File snapshots** injected into the prompt - reused if the file hasn't changed
- **Bulletin board contents** - shared findings across agents

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
2. **Avoid dynamic timestamps in system prompts.** Injecting `datetime.now()` into the system prompt creates a unique prompt on every spawn - zero cache hits.
3. **Pass shared context as early context.** Files referenced in the first 1024+ tokens are cached; files appended late are not.
4. **Use `context_files` in `bernstein.yaml`** to preload stable reference files that all agents share:

```yaml
context_files:
  - README.md
  - docs/architecture/ARCHITECTURE.md
  - src/bernstein/core/models.py
```

### Reading cache metrics

```bash
bernstein cost --json | jq '.cache_hit_rate'
```

The `cache_hit_rate` field is a percentage (for example, `72.2`). It is `null` when there are no cache observations.

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
- True filesystem isolation - agents cannot step on each other's uncommitted changes.
- Merge happens only at task completion, not continuously.
- Supports sparse checkout for monorepos (only relevant paths checked out).

**Disadvantages:**
- Each worktree consumes disk space proportional to the working tree size.
- Symlinks (`.venv`, `node_modules`) reduce duplication but require Developer Mode on Windows.

```yaml
# bernstein.yaml - tune worktree setup
worktree_setup:
  symlink_dirs:
    - .venv
    - node_modules
  copy_files:
    - .env
  setup_command: null   # e.g., "npm install" or "uv sync"
```

`copy_files` entries are copied into each new worktree, and each one is also
excluded from git **in that worktree only**, so an agent's `git add -A` cannot
commit its copy and break the merge back. The exclusion lives in the
worktree's own git dir (a per-worktree `core.excludesFile`), never in the
shared `.git/info/exclude`, so your own checkout and other worktrees still see
the file. Entries are anchored to the repository root, so `secrets.env` does
not hide a tracked `config/secrets.env`. List files, not directories: a
directory entry is not copied and not excluded.

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

After a crash or `SIGKILL`, orphaned worktrees accumulate. Inspect them first
with the read-only command described in [worktree operations](worktrees.md):

```bash
bernstein worktrees list --json
```

Both cleanup commands select worktrees whose sessions no longer back active
tasks. Removing a selected worktree also attempts to delete its
`agent/<session>` branch, after best-effort salvage and graveyard preservation.
This applies even without `--force`.

The subsequent branch hygiene preserves other unmerged agent branches by
default. The `--force` flag also permits deleting those remaining branches,
even when their commits are not merged into `main`. Branches belonging to
active sessions stay protected. Inspect the inactive worktrees and branches
for work you want to keep before either cleanup command; preservation is
best-effort.

```bash
bernstein cleanup            # removes inactive worktrees and their branches
bernstein cleanup --force    # also permits pruning other unmerged agent branches
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
curl http://127.0.0.1:8052/status | jq '{summary, agents}'
```

`GET /status` builds these panels in
`src/bernstein/core/routes/status_dashboard.py`. It does not expose a
`metrics` object. The targets below are operator heuristics, not enforced
thresholds or guarantees.

| Report field | Source and meaning | Operator target / action |
|---|---|---|
| `summary.open`, `summary.claimed`, `summary.done`, `summary.failed` | Task-store status counts in `/status`. | Investigate a growing backlog or failed count across snapshots. |
| `summary.agents`, `summary.max_agents` | Live agent count and the configured slot limit in `/status`. | Compare available capacity with ready work before changing the limit. |
| `agents.items[].status`, `agents.items[].task_ids`, `agents.items[].runtime_s` | Normalized agent rows in `/status`; runtime is time since spawn. | Inspect long-lived workers alongside their assigned tasks; runtime is not task duration. |
| `summary.cost_usd` | Accumulated spend reported by `/status`. | Compare spend against the run's budget. |

Prometheus exposition is a separate `/metrics` endpoint, described below;
these status fields are JSON report paths, not Prometheus metric names.

### Prometheus

The task server exposes `GET /metrics` in Prometheus exposition format on its own port (default 8052); there is no separate metrics config block. Point your Prometheus scraper (which itself typically listens on 9090) at the task server:

```yaml
# prometheus.yml (scraper config, not bernstein.yaml)
scrape_configs:
  - job_name: bernstein
    static_configs:
      - targets: ["localhost:8052"]
```

Metrics at `http://localhost:8052/metrics`. A ready-made scrape config lives in `deploy/prometheus/`, and Grafana dashboards are included in `deploy/grafana/`.

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

Use the isolated test runner:

```bash
uv run python scripts/run_tests.py -x
```

The runner shards pytest invocations to keep per-shard memory bounded.

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
