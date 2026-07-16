# Model Routing & Escalation

**Does Bernstein actually escalate failed tasks to a more capable model?**

Yes. Bernstein ships **two distinct cascade systems**, both 100%
implemented and wired into the orchestrator:

| | System 1 - `CascadeRouter` | System 2 - `CascadeFallbackManager` |
|---|---|---|
| Scope | Intra-Claude tier escalation | Cross-adapter failover |
| Trigger | Task failure, janitor failure, low-confidence output | Rate limit, timeout, provider error |
| Walks | `sonnet → opus` (and historical `haiku → sonnet → opus`) | `opus → sonnet → codex → gemini → qwen` |
| Source | `core/routing/cascade_router.py` | `core/routing/cascade.py` |

This document explains both, the configuration knobs, and the
observability surface. For the higher-level provider-policy controls
(allow/deny lists, preferred provider) see `operations/MODEL_POLICY.md`.

---

## System 1 - Intra-Claude tier escalation (CascadeRouter)

`CascadeRouter` (`src/bernstein/core/routing/cascade_router.py`)
selects the cheapest viable Claude tier for a task's first attempt and
escalates only when post-hoc signals warrant it.

### When it runs

The router is consulted when the chosen adapter is Claude-compatible
(`router_applicable()` - checks the adapter name against the
`{claude, claude code, claude_code, claude-code}` set). For every other
adapter the router is skipped - its arms (`sonnet`, `opus`) are
Claude-specific.

### The tier ladder

`CASCADE` is defined in `src/bernstein/core/cost/cost.py`:

```python
CASCADE: list[str] = ["sonnet", "opus"]
```

`_cascade_for_task()` (`cascade_router.py`) returns the same
list for both standard and high-stakes paths today. Historical builds
(and the docstring at `cascade_router.py`) included `haiku → sonnet
→ opus` for standard tasks and `sonnet → opus` for high-stakes; the
haiku tier was dropped after observing that on the Anthropic Max plan
sonnet is unlimited and produces measurably better results
(`cost.py`).

A task is treated as high-stakes when **any** of:

- `task.role` ∈ `{"manager", "architect", "security"}`
- `task.complexity == Complexity.HIGH`
- `task.scope == Scope.LARGE`
- `task.priority == 1` (highest)

(See `cascade_router.py` and `_cascade_for_task`.)

### Initial selection

`select(task)` (`cascade_router.py`) returns a `CascadeDecision`:

```python
@dataclass
class CascadeDecision:
    model: str           # e.g. "sonnet"
    effort: str          # "low" | "high" | "max"
    attempt_number: int  # 0 for first attempt
    is_escalated: bool
    reason: str          # human-readable explanation
    estimated_cost_usd: float
    chain_id: str        # opaque id; pass back to record_and_escalate()
```

`_select_initial_model()` (`cascade_router.py`) decides:

1. High-stakes role/complexity/scope/priority → start at `cascade[0]`
   (currently `sonnet`).
2. Manager-supplied `task.model` override → use it if it appears in
   the cascade.
3. Otherwise consult the bandit (next section). If the cheapest tier
   has been observed `≥ MIN_OBSERVATIONS` times and its observed
   `success_rate < QUALITY_THRESHOLD`, **skip it** and try the next
   tier - a proactive cost-then-quality move.

### Escalation triggers

After the agent finishes, `record_and_escalate()` (`cascade_router.py`)
records the attempt and decides whether to retry on a higher tier.
Triggers, evaluated in order by `_should_escalate()`:

1. **Hard task failure.** `attempt.success == False` with no other info
   → escalate.
2. **Janitor verification failure.** `janitor_passed is False` → escalate.
3. **Low-confidence output.** A regex over the last 2 000 chars of agent
   output (`_LOW_CONFIDENCE_PATTERN`) matches phrases like
   "I'm not sure", "I cannot determine", "partial implementation", "left
   as placeholder", "TODO: escalat...". `detect_low_confidence()` → escalate.
4. **Late explicit failure.** `attempt.success == False` even after
   janitor or output checks → escalate.

If escalation fires and the chain is not already at the top of the
cascade (`current_idx < len(cascade) - 1`), the router
returns a new `CascadeDecision` for the next tier. Otherwise the
chain ends and the failure stands.

### The bandit

`EpsilonGreedyBandit` lives in `src/bernstein/core/cost/cost.py`. It
keeps one `BanditArm` per `(role, model)` pair, recording observations
(success/failure, cost, latency) over the run. Constants:

```python
EPSILON = 0.1            # 10% explore, 90% exploit
MIN_OBSERVATIONS = 5     # arms trusted only after this many samples
QUALITY_THRESHOLD = 0.80 # min success_rate to consider an arm
```

(`cost.py`.)

`record_and_escalate()` calls `_record_bandit()` after every
attempt, so the bandit's view of each arm improves with use. The
proactive-skip rule in `_select_initial_model()` reads
`arm.success_rate` and `arm.observations` directly.

A new arm starts with a pessimistic `success_rate = 0.5` (`cost.py`)
so a freshly added cheap model cannot greedily win selection on its
first observation - it has to earn the trust through real successes.

The bandit's persisted state is loaded lazily via `EpsilonGreedyBandit.load(metrics_dir)`
(`cascade_router.py`) and saved on demand via `save_bandit()`.

### Persistence - `cascade_chains.jsonl`

`save_chain(chain_id, task, metrics_dir)` (`cascade_router.py`)
appends one JSON line per completed chain to:

```text
.sdd/metrics/cascade_chains.jsonl
```

Sample line (whitespace added for readability):

```json
{
  "timestamp": 1714824312.5,
  "chain_id": "a1b2c3d4e5f6789a",
  "task_id": "T-042",
  "role": "backend",
  "attempts": [
    {"model": "sonnet", "effort": "high", "attempt_number": 0,
     "cost_usd": 0.042, "latency_s": 87.3, "success": false,
     "escalated": true,
     "escalation_reason": "low-confidence signal in output: 'partial implementation'"},
    {"model": "opus", "effort": "max", "attempt_number": 1,
     "cost_usd": 0.612, "latency_s": 156.0, "success": true,
     "escalated": false}
  ],
  "final_model": "opus",
  "succeeded": true,
  "total_cost_usd": 0.654,
  "first_attempt_cost_usd": 0.042,
  "escalation_overhead_usd": 0.612,
  "saved_vs_direct_opus_usd": 0.546
}
```

Aggregate stats are computed on demand by
`load_cascade_savings_summary(metrics_dir)` (`cascade_router.py`):

```python
{
  "total_chains": 1247,
  "total_cost_usd": 412.93,
  "escalation_overhead_usd": 28.41,
  "saved_vs_opus_usd": 1153.04,
  "escalation_rate": 0.073   # 7.3% of chains escalated past tier 0
}
```

---

## System 2 - Cross-adapter failover (CascadeFallbackManager)

`CascadeFallbackManager` (`src/bernstein/core/routing/cascade.py`)
handles a different problem: the chosen provider is **unavailable** (rate
limit, timeout, API error) and we need to redirect the task to a
different provider entirely.

### Default cascade order

`DEFAULT_CASCADE_ORDER` (`cascade.py`):

```python
DEFAULT_CASCADE_ORDER: list[str] = ["opus", "sonnet", "codex", "gemini", "qwen"]
```

Each entry resolves to a provider via `_MODEL_TO_PROVIDER`
(`cascade.py`). When `find_fallback()` is called, it
walks this list starting **after** the current entry and returns the
first viable agent.

### When it runs

Called by the orchestrator/spawner when an agent process raises
rate-limit (HTTP 429), timeout, or generic API error
(`cascade.py` lists the triggers). The `trigger` argument records
which condition fired so metrics can split by cause.

### Capability floor

Cross-adapter fallback never violates the task's reasoning floor -
fallback to a weak model just because the strong one is throttled
would silently degrade output quality:

```python
CAPABILITY_FLOOR: dict[Complexity, int] = {
    Complexity.HIGH:   _STRENGTH_ORDER["high"],     # only high or very_high
    Complexity.MEDIUM: _STRENGTH_ORDER["medium"],
    Complexity.LOW:    _STRENGTH_ORDER["low"],
}
```

(`cascade.py`.) Candidates below the floor are skipped
(`_is_viable_candidate`).

### Sticky fallback

To prevent ping-pong between primary and fallback, once a fallback is
selected it sticks for `_DEFAULT_STICKY_DURATION_S = 300.0` seconds
(`cascade.py`). `find_fallback()` checks
`get_sticky_fallback()` first and reuses it when the
window has not yet expired.

### Cascade chain inspection

`find_fallback_chain(complexity, initial_provider)` (`cascade.py`)
walks the entire cascade and returns the full sequence of fallback
decisions that **would** be tried if each provider in turn were
rate-limited. Useful for audit logs and debugging.

---

## Configuration

### YAML (`bernstein.yaml`)

```yaml
quality_gates:
  enabled: true   # cascade router gates on janitor + lint + tests
  lint: true

# Per-role default model and effort. CascadeRouter reads task.model /
# task.effort from this when the manager has not set them.
role_model_policy:
  manager:  { cli: claude, model: opus,   effort: max }
  backend:  { cli: claude, model: sonnet, effort: high }
  qa:       { cli: claude, model: sonnet, effort: high }

# Provider-level gating runs *before* either cascade.
# See operations/MODEL_POLICY.md.
model_policy:
  allowed_providers: [anthropic, openai, google]
  prefer: anthropic
```

### CLI flags (`src/bernstein/cli/main.py`)

| Flag | Effect |
|------|--------|
| `--routing {default,bandit}` | Pick the bandit or the static-cost router. |
| `--model <name>` | Override the initial model for this run. |
| `--budget <usd>` | Cap per-run spend; cascade refuses to escalate past it. |

---

## Observability

### `cascade_chains.jsonl`

Schema documented above. The simplest way to inspect:

```bash
jq '. | {role: .role, attempts: (.attempts | length), saved: .saved_vs_direct_opus_usd}' \
  .sdd/metrics/cascade_chains.jsonl | tail
```

### `/routing/bandit` HTTP endpoint

Exposed by `core/routes/status_dashboard.py`:

```text
GET http://localhost:<port>/routing/bandit
```

Returns per-(role, model) bandit state read from `.sdd/routing/`
(populated when `--routing bandit` is active). Sample shape:

```json
{
  "active": true,
  "mode": "bandit",
  "arms": {
    "backend|sonnet": {"observations": 124, "success_rate": 0.91,
                       "avg_cost_usd": 0.039, "avg_latency_s": 78.4},
    "backend|opus":   {"observations": 12,  "success_rate": 1.00,
                       "avg_cost_usd": 0.61,  "avg_latency_s": 142.7}
  }
}
```

Empty `{}` when bandit routing has not been activated.

### Logs

The router logs every escalation at `INFO`:

```text
Cascade chain a1b2c3d4...: escalating sonnet → opus
  (reason: low-confidence signal in output: 'partial implementation')
```

(`cascade_router.py`.) Cross-adapter fallback logs at `INFO`
when a provider is selected and at `WARNING` when the chain is
exhausted (`cascade.py`).

### Aggregate savings summary

```python
from pathlib import Path
from bernstein.core.routing.cascade_router import load_cascade_savings_summary

summary = load_cascade_savings_summary(Path(".sdd/metrics"))
print(summary["saved_vs_opus_usd"], summary["escalation_rate"])
```

---

## How the two systems interact

A single task's run can touch both:

```text
CascadeRouter.select(task)               → sonnet  (intra-Claude)
adapter.run(...)                         → 429 from Anthropic
CascadeFallbackManager.find_fallback(    → (codex, gpt-5.4)
    excluded={anthropic}, current_entry="sonnet", trigger="rate_limit")
adapter[codex].run(...)                  → success but janitor fails
CascadeRouter.record_and_escalate(janitor_passed=False)
                                         → opus  (back inside Claude)
```

The two routers do not coordinate state. `CascadeRouter` cares about
**output quality**; `CascadeFallbackManager` cares about **provider
availability**.

---

## Code pointers

| Concern | File | Symbol / line |
|---------|------|---------------|
| Cascade router selection | `src/bernstein/core/routing/cascade_router.py` | `CascadeRouter.select:330` |
| Initial model picker | `src/bernstein/core/routing/cascade_router.py` | `_select_initial_model` |
| Escalation decision | `src/bernstein/core/routing/cascade_router.py` | `record_and_escalate`, `_should_escalate` |
| Low-confidence regex | `src/bernstein/core/routing/cascade_router.py` | `_LOW_CONFIDENCE_PATTERN`, `detect_low_confidence` |
| Tier ladder | `src/bernstein/core/cost/cost.py` | `CASCADE` |
| `_cascade_for_task` (high-stakes branch) | `src/bernstein/core/routing/cascade_router.py` | `_cascade_for_task` |
| Bandit arm | `src/bernstein/core/cost/cost.py` | `EpsilonGreedyBandit`, constants `EPSILON/MIN_OBSERVATIONS/QUALITY_THRESHOLD` |
| Proactive-skip rule | `src/bernstein/core/routing/cascade_router.py` | `_select_initial_model` |
| Chain persistence | `src/bernstein/core/routing/cascade_router.py` | `save_chain`, `CHAIN_FILE` |
| Aggregate savings | `src/bernstein/core/routing/cascade_router.py` | `load_cascade_savings_summary` |
| Cross-adapter fallback | `src/bernstein/core/routing/cascade.py` | `CascadeFallbackManager`, `find_fallback` |
| Default cascade order | `src/bernstein/core/routing/cascade.py` | `DEFAULT_CASCADE_ORDER` |
| Capability floor | `src/bernstein/core/routing/cascade.py` | `CAPABILITY_FLOOR`, `_is_viable_candidate` |
| Sticky fallback | `src/bernstein/core/routing/cascade.py` | `_DEFAULT_STICKY_DURATION_S`, `get_sticky_fallback` |
| `/routing/bandit` endpoint | `src/bernstein/core/routes/status_dashboard.py` | `bandit_routing_stats` |
| `--routing` CLI flag | `src/bernstein/cli/main.py` | - |

## Related

- `operations/MODEL_POLICY.md` - provider-level allow/deny policy that
  runs **before** either cascade decides anything.
- `architecture/quality-pipeline.md` - janitor + gates that emit the
  `janitor_passed` signal the cascade router consumes.
- `architecture/state-persistence.md` - where bandit state and cascade
  chain reports live on disk.
