# Recipes (first-class workflow library)

Audience: operators who want a parameterised workflow they can invoke
in one line instead of authoring a `WorkflowSpec` from scratch.

## Overview

A recipe is a parameterised workflow manifest. Each manifest lives at
`templates/recipes/<name>.yaml` and reuses
`bernstein.core.workflows.workflow_spec.WorkflowSpec` for the node
body. A top-level `params:` block adds operator-facing typed inputs.

The CLI validates parameters, applies defaults, and renders
placeholders before handing the resolved `WorkflowSpec` to the existing
`WorkflowRunner`.

Source:

- `src/bernstein/cli/commands/recipes_cmd.py`
- `bernstein.core.workflows.recipe_spec.RecipeSpec`
- `templates/recipes/*.yaml`

## Bundled recipes

| Name | What it does |
|------|--------------|
| `refactor-glob` | Rename an identifier pattern across a path, then run tests. |
| `bump-dependency` | Upgrade a Python dep to a target version, run tests, fix breakage, re-run. |
| `add-tests-for-module` | Survey a module's public surface and backfill pytest coverage. |
| `license-audit` | Scan deps for licenses incompatible with the project license; write report. |
| `regenerate-docs` | Refresh module map + API docs, then build and lint the docs site. |

## CLI

```text
bernstein recipes list                                       [--bundled-only]
bernstein recipes show NAME
bernstein recipes run NAME --param key=value [--param ...]   [--dry-run] [-g GOAL]
```

- `list` enumerates bundled + user-installed recipes with their
  one-line descriptions.
- `show` prints the manifest details: params (with types, defaults,
  required flags, choices), nodes, dependency order.
- `run` executes end-to-end. `--dry-run` prints the resolved workflow
  plan without spawning agents.

## Registered runs (content-addressed, receipt-backed)

`recipes run` executes a manifest ad hoc. For a workflow you fire
repeatedly, register it first: the recipe id becomes the sha256 of its
canonical body, and every fire writes a receipt to the audit chain.

```text
bernstein recipes register NAME  [--collision-policy enqueue|cancel_new|supersede_with_handoff]
                                 [--concurrency-cap N] [--sandbox-pool POOL]
bernstein recipes fire NAME      [--at UNIX_EPOCH] [-g GOAL] [--schedule SCHEDULE_ID]
bernstein recipes history NAME   [--verify]
bernstein recipes repair-lineage NAME [--pick HMAC]
```

- `register` seals the canonical bytes into the lineage spine and writes
  a register receipt (or, for a changed body, an operator-signed
  supersede receipt). It prints the `recipe_hash` and `spine_anchor`.
- `fire` submits the recipe's work and prints the fire receipt: the
  projection hash and the chain anchor, not an opaque job id. The
  receipt is the response.
- `history` walks the definition-lineage receipts. `--verify` checks
  them against the HMAC audit chain offline (no server running); a
  broken or reordered link exits non-zero.
- `repair-lineage` resolves a forked definition lineage (see below).

### `fire` needs a reachable, authenticated task server

`fire` records a fire only against work a sink actually accepted. It
submits to the task server and derives the receipt from what came back,
so a fire that could not submit its work never writes a "successful"
receipt.

The practical consequence is that `fire` now requires a task server
that is both reachable and authenticated:

| Situation | Result |
|-----------|--------|
| Recipe dispatched and work accepted | exit `0`; prints the fire receipt |
| Recipe is paused (a deliberate operator state) | exit `0`; nothing fired |
| No task server reachable, or auth rejected (e.g. `401`) | exit `2`; no receipt |
| Recipe not found, or load/registration error | exit `1` |

Exit `2` is deliberate: a script must never read a failed submission as
a successful run. If you previously fired with no task server running
and got exit `0`, start (and authenticate against) the task server
first.

### Recovering a forked lineage

The definition lineage is an append-only chain, so recovery is
additive. A fork means one receipt has two successors; the projection
cannot honestly pick one, so every operation on the name fails closed
with a message pointing at `recipes repair-lineage`.

```bash
# 1. List the competing branches (no --pick):
bernstein recipes repair-lineage my-recipe

# 2. Follow one by naming its receipt hmac (or 16-char prefix):
bernstein recipes repair-lineage my-recipe --pick <hmac>
```

The choice is itself a receipt. Nothing is deleted: the losing branch
stays in the history (`recipes history`), and re-running with the other
hmac wins, because the latest resolution is authoritative. Without
`--pick`, listing the branches exits `1` so a script does not read an
unresolved fork as resolved.

## Manifest schema

Each manifest declares:

| Section | Use |
|---------|-----|
| `name`, `description`, `version` | Recipe identity |
| `params:` | Typed inputs (`string`, `int`, `float`, `bool`) with `required`, `default`, `choices`, `help` |
| `nodes:` | Standard `WorkflowSpec` nodes; placeholders reference params as `{{ name }}` |

Bad input -> exit `1` with an operator-readable error. Bad manifest ->
exit `2`.

## Examples

Run a glob-rename then test:

```bash
bernstein recipes run refactor-glob \
  --param pattern=foo_ \
  --param replacement=bar_ \
  --param path=src/bernstein \
  --param test_command="pytest -x tests/unit"
```

Dry-run a dependency bump:

```bash
bernstein recipes run bump-dependency \
  --param package=httpx \
  --param version=0.27.0 \
  --dry-run
```

Show the parameter shape of a recipe:

```bash
bernstein recipes show bump-dependency
```

List only bundled recipes (skip user-installed):

```bash
bernstein recipes list --bundled-only
```

## Authoring your own

Drop a YAML file under `templates/recipes/` (or any user-recipe path
the CLI scans). Required header: `name`, `description`, `version`,
`params`, and the standard `WorkflowSpec` body. Re-run
`bernstein recipes list` to confirm pickup.

## Troubleshooting

**`bad param: <name>`.** The value did not match the declared type or
the `choices` whitelist. Re-check `bernstein recipes show <name>` for
the canonical shape.

**`recipe not found`.** Either the file is outside the scanned
directories or the YAML failed to parse. `bernstein recipes list`
prints the resolved paths it scanned.

**Run "succeeds" but no diff appears.** You probably ran with
`--dry-run`. Drop the flag to actually spawn agents.
