# Task-tier classification at dispatch

Bernstein can optionally map a task onto a small closed **tier**
(`light` | `standard` | `heavy` | `critical`) and pick the model for that
tier from `role_model_policy.<role>.tier_models`. Roles without
`tier_models` are unchanged: the single `model` pin still applies, and no
features are extracted.

This page is for operators reading a recorded decision and for anyone
tuning thresholds. Source of truth for the classifier:
`src/bernstein/core/routing/task_tier.py` (`TIER_POLICY_VERSION`).

## Why

`role_model_policy` pins one model per role. A one-line docs fix and a
cross-module refactor therefore pay the same price unless the operator
encodes the distinction by hand. Task-tier classification runs at
dispatch (before adapter selection), is pure, and leaves a replayable
record of *why* a tier was chosen.

## Opt-in config

```yaml
role_model_policy:
  backend:
    model: sonnet          # fallback when tier is unmapped or classifier errors
    tier_models:
      light: haiku
      standard: sonnet
      heavy: opus
      critical: opus
```

- `tier_models` keys must be members of the closed tier set. The reserved
  classifier marker `error` is **not** a valid key (config validation
  refuses it) so a broken classifier cannot be configured as a cheap tier.
- Partial maps are allowed: an unmapped tier falls back to `model`.
- Omit `tier_models` entirely for byte-identical pre-change dispatch.

## Features (policy version 1)

| # | Feature | Meaning | Fallback when absent |
|---|---------|---------|----------------------|
| 1 | `size_rank` | Ordinal of an issue `size/*` label (`xs`=0 … `xl`=4) | `2` (`m`) |
| 2 | `file_count` | Number of path strings on the task surface | `0` |
| 3 | `test_touched` | `1` if any path looks like a test | `0` |
| 4 | `code_file_count` | Paths that are not documentation-only | `0` |
| 5 | `symbol_nodes` | AST symbol-graph node count when supplied | `0` |

Paths are taken from `Task.owned_files` and, when present,
`metadata.changed_files` / `files` / `paths`. Labels come from
`metadata.labels` / `issue_labels` / `pr_labels` and `Task.tags`. Symbol
counts come from `metadata.symbol_graph.node_count` (or
`symbol_node_count`). Missing artefacts never raise.

### Score and bands

```text
score = size_rank + file_count + 2*test_touched + code_file_count
        + min(symbol_nodes, 50)//10
```

Inclusive lower bounds; the **highest** matching band wins (a score exactly
on a boundary is assigned that band):

| Tier | Condition |
|------|-----------|
| `light` | `score < 4` |
| `standard` | `score >= 4` and `< 10` |
| `heavy` | `score >= 10` and `< 18` |
| `critical` | `score >= 18` |

Thresholds are literals under `TIER_POLICY_VERSION`. Change them only with a
version bump (derive offline from dispatch history if you like — never at
runtime).

## Reading a recorded decision

On the dispatch seam next to adapter capability selection the audit chain
gains a `task.tier_decision` event:

| Field | Meaning |
|-------|---------|
| `tier` | Chosen band, or `error` if the classifier raised |
| `tier_policy_version` | Classifier version at decision time |
| `feature_digest` | SHA-256 of version + ordered feature vector |
| `features` | The five ints above |
| `score` | Scalar that selected the band |

Replay recomputes the classification. A bumped
`tier_policy_version` is reported as
`tier_policy_version diverged: recorded=… current=…` rather than a generic
model mismatch.

## Defensive behaviour

If classification raises despite the totality property (a bug), the call
site records `tier: error` and dispatches exactly as an unmapped role
would (`model` pin). A broken classifier must never read as a cheap-tier
verdict.

## Related

- Classifier: `src/bernstein/core/routing/task_tier.py`
- Recording: `record_task_tier_decision` in `src/bernstein/core/security/audit_chain.py`
- Selection seam: `route_and_record` in `src/bernstein/adapters/capability_profile.py`
- Model routing overview: [model-routing.md](../architecture/model-routing.md)
