# Per-step CLI and model routing

**How do I run one step with one CLI agent and the next step with a different one?**

Bernstein supports per-step routing on plan YAML files and on tasks
posted directly to the task server. A step can pin its own adapter
(`cli:`) and its own model (`model:` / `effort:`), and those hints win
over the top-level plan-wide defaults. Use this when one stage of the
work needs a different runtime than the rest of the plan, for example
running red/green/refactor on one adapter and the review pass on
another.

This page covers what the per-step fields do, where they are honoured,
where they are silently dropped, and how to verify the route the
orchestrator actually took.

---

## TL;DR

| Field | Type | Scope | Wins over |
|-------|------|-------|-----------|
| `cli` | string | One step | Top-level plan `cli:` and role policy. |
| `model` | string | One step | Cascade router initial pick. |
| `effort` | enum | One step | Cascade router effort default. |

- Set them inside `steps:` in a plan YAML.
- Leave them off when the step should follow the plan-wide default.
- `bernstein.yaml` itself takes a top-level `cli:` only; the per-step
  override lives on each plan step.
- Workflow manifests under `templates/workflows/*.yaml` accept the same
  fields on agent nodes; see [Where it works](#where-it-works) below.

---

## What the fields mean

### `cli`

Pins the adapter for one step. Accepts any name the adapter registry
knows: `claude`, `codex`, `gemini`, `qwen`, `opencode`, `cursor`,
`copilot`, and so on (see the full list with `bernstein adapters list`).
A per-step `cli:` overrides:

1. The top-level `cli:` in the plan or in `bernstein.yaml`.
2. The role default supplied by the model-routing policy.
3. The `auto`-detection result.

If the named adapter is not installed, the run fails fast with a clear
error rather than silently substituting a different one.

### `model`

Pins the provider-specific model identifier for one step, such as
`provider/model-name`. When set,
the cascade router skips its initial selection logic and uses the
pinned model as attempt 0.

### `effort`

Pins the effort tier for one step: `low`, `normal`, `high`, `max`.
This is a Claude-adapter knob that maps onto the inline reasoning
budget. Like `model:`, it is honoured when the adapter is
Claude-compatible.

---

## Worked example

Discussion #962 asks the canonical question: red/green/refactor on one
adapter, review on another. Express that in a plan:

```yaml
name: rgr-with-review
description: >
  Red/green/refactor on opencode; bring claude in for the review pass.

cli: opencode   # plan-wide default

stages:
  - name: rgr
    steps:
      - title: "Red: write the failing test"
        role: qa
        # cli omitted on purpose: this step inherits the plan-wide
        # opencode adapter so the test is authored by the same runtime
        # that will implement the fix below.

      - title: "Green: make it pass"
        role: backend
        # cli also inherited from the plan-wide default.

      - title: "Refactor: tighten the implementation"
        role: backend
        # still on opencode.

  - name: review
    depends_on: [rgr]
    steps:
      - title: "Independent review pass"
        role: reviewer
        cli: claude          # override: review uses a different runtime
        model: opus          # high-stakes review pinned to opus
        effort: high
```

Three steps inherit `cli: opencode`. The fourth pins `cli: claude`
plus `model: opus` and `effort: high` so the review pass runs on a
different adapter and a heavier model than the rest of the plan. No
manager agent needed; the plan is the decomposition.

---

## Where it works

| Surface | Per-step `cli:` / `model:` / `effort:` | Notes |
|---------|----------------------------------------|-------|
| Plan YAML (`bernstein run --from-plan`) | Yes | Read in `plan_loader._parse_step`. Stored on the resulting `Task` row. |
| `POST /tasks` on the task server | Yes | The HTTP payload accepts the same keys; the planner forwards them when it creates child tasks (`planner.py:86`). |
| Manager-emitted plans | Yes | When the manager agent decomposes a goal it can stamp `cli` and `model` on individual steps. |
| Workflow manifests (`templates/workflows/*.yaml`) | Yes, on agent nodes | The manifest schema validates the fields and the runner forwards them to the spawned `Task`. |
| `bernstein.yaml` | Top-level only | The seed file has one global `cli:`. Per-step routing belongs on the plan or task. |

Plans and workflow manifests are sibling primitives: use a plan when the
stage-oriented plan loader is the better fit, or a workflow manifest when
you want an explicit node DAG. Both surfaces preserve routing hints on the
resulting task.

---

## When the hint is forwarded vs. dropped

The orchestrator forwards `cli` / `model` / `effort` in these
places:

1. **Plan ingest.** `plan_loader._parse_step` reads them off each step
   dict and writes them onto the `Task` (`plan_loader.py:255-294`).
2. **Planner-emitted child tasks.** When the planner posts derived
   tasks to the task server, it includes `cli`, `model`, and `effort`
   in the JSON body (`planner.py:91-96`). A regression in this exact
   path was fixed in PR #1259 and pinned by
   `tests/unit/test_per_step_routing.py`.
3. **Spawner dispatch.** The agent spawner reads `task.cli` and
   resolves the adapter from the registry before launching the
   process; if `task.cli` is unset it falls back to the role policy
   plus the orchestrator-wide default.

The hint is dropped (silently or with a warning) in these cases:

- The adapter named in `cli:` is not installed. The run aborts with a
  registry-miss error; it does **not** substitute `auto`.
- An adapter does not expose a flag for one of the requested hints. The
  task still records the requested value; adapter-specific support is
  visible in the actual spawn event in the trace (next section).
- Command nodes carry `cli:`, `model:`, or `effort:`. These fields only
  apply to agent nodes, so the manifest loader rejects them rather than
  silently ignoring the configuration.

---

## How to verify the route in the trace

Every task spawn writes one JSONL event per attempt under
`.sdd/traces/<task_id>.jsonl`. The relevant fields:

| Field | Meaning |
|-------|---------|
| `task.cli` | The adapter override on the task row. Empty when unset. |
| `task.model` | The model override on the task row. Empty when unset. |
| `task.effort` | The effort override on the task row. Empty when unset. |
| `spawn.adapter` | The adapter actually launched. Compare to `task.cli`. |
| `spawn.model` | The model the adapter reported it loaded. |

Two quick checks:

```bash
# Show the override on every task in the run.
jq -r '[.task_id, .task.cli, .task.model, .task.effort] | @tsv' \
  .sdd/traces/*.jsonl | sort -u

# Confirm each spawn used the requested adapter.
jq -r 'select(.event=="spawn") | [.task_id, .task.cli, .spawn.adapter] | @tsv' \
  .sdd/traces/*.jsonl
```

If `task.cli` is `claude` but `spawn.adapter` is `codex`, the run hit
an adapter-resolution fallback (almost always: `claude` is not
installed or not on `PATH`). The `bernstein doctor` output names the
exact registry miss.

---

## See also

- [Plans (YAML schema)](../architecture/plans.md) - full schema for
  the plan file, including the rest of the step fields.
- [Model routing and escalation](../architecture/model-routing.md) -
  what the cascade router does when `model:` is not pinned.
- `templates/plan.yaml` - the in-repo seed file with the commented
  `cli:` override example.
