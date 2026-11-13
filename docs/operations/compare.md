# Compare (side-by-side adapter A/B)

Audience: operators picking between adapters (Claude vs Codex vs Aider
vs Gemini) on the same task, without burning two manual runs.

## Overview

`bernstein compare` runs the same task spec in input order against up to
**four** adapters, waiting for each adapter to finish before starting the
next, in isolated per-adapter working copies. It diffs the produced changes
against the baseline workspace and writes a JSON sidecar plus a Markdown
summary.

The single-adapter `bernstein run` path is untouched. The compare
runner is a separate, deterministic harness.

Source:

- `src/bernstein/cli/commands/compare_cmd.py`
- `src/bernstein/core/orchestration/compare_runner.py`
- `src/bernstein/eval/telemetry.py` (adds `compare_run_id`)

## CLI

```text
bernstein compare SPEC_PATH --adapters NAME[,NAME...]
  [--workspace DIR]      baseline snapshot; defaults to cwd
  [--role NAME]          role applied identically to all adapters; default backend
  [--seed N]             deterministic seed forwarded to adapters; default 0
  [--keep-worktrees]     keep per-adapter worktrees on disk after the run
  [--traces-dir DIR]     override JSON sidecar dir; default .sdd/traces
  [--no-sidecar]         skip writing the JSON sidecar
```

Cap is 4 adapters per run. Asking for more exits with `2` and an
explicit error. Duplicates are rejected. The degenerate 1-adapter case
is allowed for harness symmetry.

Adapter results are processed in the order given to `--adapters`. The
runner waits for each adapter to complete before invoking the next; adapters
do not run in parallel.

## Outputs

- **stdout** - Markdown summary table (one row per adapter) plus
  per-file diff blocks.
- **JSON sidecar** - `compare-<id>.json` in the selected traces directory.
  By default this is `.sdd/traces` under the current working directory;
  `BERNSTEIN_TRACES_DIR` overrides that default, and `--traces-dir DIR`
  explicitly selects the output directory (taking precedence over the
  environment variable). Skipped with `--no-sidecar`; the comparison and
  Markdown summary still run.
- **Telemetry** - `AgentTelemetry` rows carry `compare_run_id` so
  downstream eval ingestion can group runs.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | At least one adapter exited cleanly; other adapters may have failed |
| Non-zero | All adapters failed (`exit_code != 0` for every adapter) |

Per-adapter failures are always rendered in the summary regardless of
the overall exit code. In particular, a zero exit code does not mean every
adapter succeeded.

## Examples

Two-adapter A/B:

```bash
bernstein compare ./task-spec.md --adapters claude,codex
```

Four-way bake-off, keep worktrees for offline inspection:

```bash
bernstein compare ./task-spec.md \
  --adapters claude,codex,gemini,aider \
  --keep-worktrees
```

Throwaway smoke run, no sidecar:

```bash
bernstein compare ./task-spec.md --adapters claude --no-sidecar
```

## Worktree hygiene

Each adapter gets its own working copy under a unique directory in the
system temporary directory: `bernstein-compare-<compare_run_id>-<suffix>/`
with one `<adapter>/` working copy per adapter.
The runner clones the baseline workspace into each copy with
`shutil.copytree`, runs the adapter, computes a unified diff against the
baseline, and then removes the temporary root after the run unless
`--keep-worktrees` is set. Keeping worktrees leaves the per-adapter copies
on disk for inspection of each adapter's resulting files; it does not
change the comparison or its diffs.

The sidecar is written as `compare-<compare_run_id>.json` in
`$PWD/.sdd/traces` by default. `BERNSTEIN_TRACES_DIR` changes the default
output directory, while `--traces-dir DIR` explicitly selects one and takes
precedence over the environment variable. `--no-sidecar` suppresses only
this JSON file, not the comparison or its Markdown summary.

## Troubleshooting

**`unknown adapter: X`.** The adapter is not registered. Run
`bernstein integrations list` to list registered adapters; install / register
the missing one or drop it from `--adapters`.

**`--adapters cap is 4; got N`.** Trim the list. The cap is hard.

**Adapter binary missing.** The runner does not crash; instead it
reports a per-adapter `error` row in the summary so the comparison
still shows the rest of the bake-off.
