# Task-tier classification at dispatch

## Backend dispatch tier

The task API field `dispatch_tier` selects the backend tier and maps directly
to `Task.dispatch_tier` (`DispatchTier`). Accepted strings are `local`, `edge`,
`search`, `research` and `sandbox`. Omission defaults to `local`; explicit
`null`, other types and unknown strings are validation errors (HTTP 422 on
REST creation). There is no `tier` alias.

The shared request builder, single and batch creation, REST responses, JSONL
and PostgreSQL retain this field. Storage writes the enum value as a string
and restores the enum on read. Legacy fieldless JSONL tasks and existing
PostgreSQL rows default to `local`. JSONL replay keeps the latest explicitly
recorded value even if a later record omits it; existing records are not
rewritten. PostgreSQL adds the column through an idempotent startup migration.

This backend field is separate from the model classification below
(`light`, `standard`, `heavy`, `critical`); it does not change classification
or provider routing.

## Model classification

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

When reproducing the pure `extract_features` result, preserve input order and
entries. The first recognized `size/{xs,s,m,l,xl}` label wins after trimming
surrounding whitespace and ignoring case. Each nonblank path string counts
separately, including repeated paths. Backslashes are normalized to slashes. The test
flag is set to `1` if any path contains a component named `test`, `tests`
or `testing`, or if its filename has the form `test_<name>.py` or
`<stem>_test.py`. Matching is case-insensitive. The name in the prefix form
must be nonempty. The stem in the suffix form must contain one or more
Unicode alphanumeric characters or underscores. Both filename patterns also
match before a single final line feed. For example, `src/order_flow_test.py`
sets the flag and `src/order-flow_test.py` leaves it unset. Documentation-only paths are identified by a
case-insensitive final suffix of `.md`, `.mdx`, `.rst`, `.txt`, `.adoc` or
`.markdown`. They still count in `file_count` and can set `test_touched`, but
do not contribute to `code_file_count`; all other nonblank paths do.

For a worked check against the tests, `test_size_label_and_files_raise_tier`
uses `size/xl`, eight `src/aN.py` paths, `tests/unit/test_a.py`, and 40 symbol
nodes. That yields `(size_rank, file_count, test_touched, code_file_count,
symbol_nodes) = (4, 9, 1, 9, 40)` and score `4 + 9 + 2 + 9 + 4 = 28`
(`critical`, within that test's asserted `{heavy, critical}` result). The
local `Task` case in `test_tier_models_selects_mapped_model` has `size/xs` and
`README.md`: its features are `(0, 1, 0, 0, 0)` and its score is `1`.
The test asserts the resulting `light` tier. These test inputs also show why documentation paths remain
in the file total but not the code total.

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

## Task grouping and dispatch tiers

Tasks with different internal `Task.dispatch_tier` values (`local`, `edge`,
`search`, `research`, `sandbox`) never merge, even when their role, CLI and
model match. This applies to file affinity (including transitive overlaps),
agent affinity, batch packing and later small-task compaction. Equal tiers
remain eligible to merge under the existing compatibility and size rules.
These backend dispatch tiers are separate from the classifier's
`light` / `standard` / `heavy` / `critical` bands above.

## Related

- Classifier: `src/bernstein/core/routing/task_tier.py`
- Recording: `record_task_tier_decision` in `src/bernstein/core/security/audit_chain.py`
- Selection seam: `route_and_record` in `src/bernstein/adapters/capability_profile.py`
- Model routing overview: [model-routing.md](../architecture/model-routing.md)
