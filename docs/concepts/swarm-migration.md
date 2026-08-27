# Swarm migration

A migration is "the same transform applied to N files" - framework
upgrades, lint-rule rollouts, API renames. The `swarm_migration`
module enumerates targets, chunks them, fans out one short-lived
agent per chunk, and reduces the results back into a single report.
Map-reduce, deterministic, one CLI command.

## Why it exists

`task_splitter` decomposes a parent task into 2-5 subtasks via the
manager LLM. That's the right shape for normal feature work. It is
the **wrong** shape for a migration touching hundreds of files where
each file gets the same prompt - the LLM-driven split is wasted
tokens and the LLM-chosen chunks are worse than a deterministic glob.

Swarm migration is a separate first-class entry point that skips the
manager planning loop and dispatches directly.

## How to use it

```bash
# Dispatch 20 parallel agents, each rewriting up to 5 files
bernstein migrate \
    --glob 'src/**/*.py' \
    --transform 'convert all sync handler functions to async' \
    --chunk-size 5 \
    --max-parallel 20

# Pin a stable id for idempotent re-runs, and preview without spawning
bernstein migrate \
    --glob 'src/**/*.py' \
    --transform 'convert all sync handler functions to async' \
    --id sync-to-async \
    --dry-run
```

The CLI takes the glob and transform inline. Reference migration plan
YAMLs (`templates/migrations/sync_to_async.yaml`,
`templates/migrations/pytest_asyncio.yaml`) document the same shape the
`MigrationPlan` dataclass accepts:

```yaml
# templates/migrations/sync_to_async.yaml
glob: 'src/**/*.py'
transform_prompt: |
  Convert every sync function in this file to async, replacing every
  blocking `requests.X` call with `httpx.AsyncClient`. Preserve public
  signatures.
chunk_size: 5
max_parallel: 20
```

The fanout:

1. `enumerate_targets(plan, repo_root)` resolves the glob.
2. `chunk_targets(targets, chunk_size)` partitions into chunks.
3. `spawn_swarm(plan)` emits one task per chunk with role=`backend`,
   scope=`small`.
4. As each chunk task reaches a terminal state, the orchestrator
   records it in the plan's checkpoint (completed, or failed once its
   reopen budget is exhausted). Once every chunk has resolved,
   `reduce_swarm` runs automatically, aggregating pass/fail per chunk;
   the resulting `SwarmReport` is posted to the bulletin board and
   written into the checkpoint alongside the chunk records.

Re-running the same plan ID skips only chunks **verified complete**
in the checkpoint. A chunk that never ran, is still running, or
permanently failed gets a fresh task, so a restart can always make
progress on the chunks that did not finish rather than returning
whatever task id was last recorded for them.

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `--max-parallel` | `20` | Parallel agent count; clamped to the chunk count and the hard ceiling. |
| `--chunk-size` | `5` | Files per chunk. |
| `--id` | derived from the transform | Idempotency key for re-runs. |

## Limitations

- Chunks must be **independent**. Cross-file refactors (rename a
  symbol used by 200 files) need a single agent or a custom split.
- The transform logic lives in the agent's prompt - Bernstein does not
  ship code transforms.
- Rollback on partial failure uses the existing drain + worktree merge
  logic. There is no migration-specific rollback.
- Cross-chunk dependency analysis is the operator's responsibility.

## Related

- Source: `src/bernstein/core/tasks/swarm_migration.py`
- CLI: `src/bernstein/cli/commands/migrate_cmd.py`
- Plan templates: `templates/migrations/`
- [Adaptive parallelism](../architecture/adaptive-parallelism.md)
- PR #1010
