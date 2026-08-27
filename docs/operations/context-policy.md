# Context policy configuration

**New in v2.10.0** — Bernstein records what context was sent to each agent's LLM
and verifies it hasn't changed between runs. This page explains how to configure
and interpret the context policy.

## Overview

Every agent run produces a **context receipt** that fingerprints each piece of
context (role, tasks, examples, etc.) with its SHA-256 hash and estimated token
count. The receipt is stored in the run journal and audit chain so replay can
detect if the context has drifted.

## Configuration

Context policy is configured under the top-level `context` key in `bernstein.yaml`.
All fields are optional and defaults are applied if omitted.

```yaml
context:
  policy_id: "default"
  policy_version: "1"
  scope_to_agents_md: true
```

### `context.policy_id`

*Type*: `string`  
*Default*: `"default"`

A logical identifier for the context policy. The policy ID itself doesn't change
behavior — it's a label for operators to identify which context policy a run
used.

Use different `policy_id` values when you want to distinguish runs that used
different context structures (e.g., `baseline` vs `enhanced`).

### `context.policy_version`

*Type*: `string`  
*Default*: `"1"`

A version string for the context policy schema. This allows you to evolve the
context policy structure over time while maintaining compatibility.

When you change how context is collected or formatted, bump the `policy_version`
to signal that old receipts from previous versions may not be comparable.

### `context.scope_to_agents_md`

*Type*: `boolean`  
*Default*: `false`

When `true`, context is scoped to the sections declared in the project's
`.sdd/agents-md/` directory. This enables deterministic replay by ensuring that
only documented context sections are included in the receipt.

Set this to `true` when you want to enforce that every context section has a
corresponding documentation entry in `agents-md`, making it easier to audit
what was sent to the model.

#### How scoping works

1. When `scope_to_agents_md` is `false`, context sections are included in the
   receipt in the order they were constructed.
2. When `scope_to_agents_md` is `true`, only sections that appear in
   `.sdd/agents-md/` are included. Any undocumented sections are excluded from
   the receipt.

## Default behavior

If the `context` section is omitted from `bernstein.yaml`, the following defaults apply:

| Field | Default |
|-------|---------|
| `policy_id` | `"default"` |
| `policy_version` | `"1"` |
| `scope_to_agents_md` | `false` |

The context receipt is always generated with these defaults, even when no
explicit configuration is present.

## Verifying context

Run `bernstein context verify` to check that a run's context matches the
current configuration:

```bash
# Verify context for a specific task
bernstein context verify --task-id <task_id>

# Verify context for the current run
bernstein context verify --run-id <run_id>
```

If the current context policy differs from what was used during the run, the
verify command reports the diverging fields.

## Related topics

- [Context receipt schema](../reference/context-receipt.md) — The exact structure
  of the receipt record.
- [Deterministic replay](deterministic-replay.md) — How context receipts enable
  reproducible runs.
- [Run receipts](run-receipts.md) — Overview of run-level receipts and
  attestation.
