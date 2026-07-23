# Nightly real-run canary

Audience: operators who rely on agents + CI and never run the program by
hand. This canary executes a real end-to-end orchestration on a schedule
so integration-level runtime breaks surface before a user hits them.

Companion to [`ci.md`](./ci.md) (the PR gate).

## TL;DR

| Fact | Value |
|------|-------|
| Workflow | `.github/workflows/nightly-canary.yml` |
| Script | `scripts/canary_real_run.py` |
| Schedule | `41 5 * * *` (05:41 UTC daily) + manual `workflow_dispatch` |
| Posture | ADVISORY -- reports red, does NOT block merges |
| Cost | none (deterministic stub adapter, no LLM key, no network egress) |
| On failure | emits one `environment=canary` event to the error sink, exits non-zero |

## Why it exists

PR CI runs unit / property / contract tests plus an install-smoke that
checks `--version` / `--help`. None of that **executes** a real flow, so
a break in genuinely-runtime plumbing -- a worker subprocess spawn, a
`git worktree` round-trip, an audit-chain append, a lineage receipt --
stays invisible until a user trips it. The canary closes that gap.

## What it runs

Three representative real-runtime flows, each in a temp dir it owns and
tears down. These are the paths the unit suite mocks:

| Flow | Real path exercised |
|------|---------------------|
| `subprocess_spawn` | `MockAgentAdapter.spawn()` forks a real `subprocess.Popen`; the canary reaps it and asserts a clean exit + a written log |
| `git_worktree` | `WorktreeManager.create()` runs a real `git worktree add`; `.cleanup()` runs `git worktree remove`; the canary asserts no leftover worktree |
| `audit_and_lineage` | real HMAC-chained `AuditChainStore` append + `verify()`, then a real Ed25519-signed `LineageRecorder` receipt re-verified through `lineage.gate.check` (the auditor's own gate) |

Each flow runs independently: one failure does not stop the others, so a
single run surfaces every broken surface, not just the first.

## Env it needs

| Variable | Source | Effect if unset |
|----------|--------|-----------------|
| `BERNSTEIN_TELEMETRY_DSN` | operator-configured (unset in CI by default) | telemetry client is a no-op; canary still runs and still red/green on its own merits |
| `BERNSTEIN_TELEMETRY_BACKPRESSURE` | set to `queue` in the workflow | block-on-full (bounded ~1s) so the one failure event is not dropped |

No backend hostname is hardcoded anywhere. The DSN reaches the script
only through the environment; the script uses the existing observability
client (`error_capture.capture_exception`) and invents no new emitter.

## How to read a canary failure

1. Open the red **Nightly real-run canary** run. The failing flow logs
   `canary flow '<name>' failed: <exc>` with a full traceback.
2. The same failure is captured as one error event with these fields:

   | Field | Meaning |
   |-------|---------|
   | `logger` | `bernstein.canary` |
   | `tags.environment` | `canary` |
   | `tags.flow` | which flow broke (`subprocess_spawn` / `git_worktree` / `audit_and_lineage`) |
   | `tags.exc_type` | exception class name |
   | `tags.top_frame` | `file:line` of the failing frame |
   | `extra.exc_value` | one-line exception message |

3. Reproduce locally with no secrets needed:

   ```
   uv run python scripts/canary_real_run.py --verbose
   ```

   Exit `0` = all flows green; exit `1` = at least one flow failed.

## How it emits a failure event

```
canary flow raises
   -> error_capture.capture_exception(category="canary", tags={environment: canary, ...})
   -> side channel POSTs a Sentry-protocol event to the configured DSN
      (no-op when BERNSTEIN_TELEMETRY_DSN is unset)
```

The `environment=canary` tag keeps canary noise out of the production
error stream.

## How to promote it to a required check later

This workflow is advisory by design. To make a canary failure block
merges (an explicit operator decision, not a default):

1. Confirm the canary is stable over several scheduled runs (no flakes).
2. Add **Real-run canary** to branch protection's required-status-check
   list for `main`.
3. Optionally add it to the `CI gate` aggregation so the gate stub waits
   on it.

Until then it is a smoke alarm, not a gate: it tells you something broke
without holding up unrelated work.
