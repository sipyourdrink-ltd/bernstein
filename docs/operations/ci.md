# CI runbook

Operator-facing notes on Bernstein's CI workflows. Focused on the
matrix policy and the failure-class interventions; for the per-step
documentation read the inline comments in `.github/workflows/ci.yml`.

## TL;DR

| Topic | Status | Where |
|-------|--------|-------|
| Per-PR macOS matrix | Gated (#1468) | `.github/workflows/ci.yml` |
| macOS safety net | Nightly + push-on-sensitive | `.github/workflows/ci-macos-nightly.yml` |
| Required check | Single `CI gate` job | `.github/workflows/ci.yml` |
| Concurrency | PR-scoped cancel, push-scoped non-cancel | `.github/workflows/ci.yml` |

## macOS matrix policy (closes #1468)

### Why

GitHub-hosted `macos-latest` runners are the long-tail bottleneck. On
2026-05-18 they queued 20-70 minutes during burst-merge waves while
ubuntu and windows cleared their normal SLO. Per-PR macOS was the
dominant cause; macOS-specific code surface is small (a dozen modules
with `sys.platform == "darwin"` branches).

### What runs when

| Event | macOS jobs trigger? | Notes |
|-------|---------------------|-------|
| `push` to `main` | Always | Every merged commit gets a fresh macOS signal |
| PR with `macos-needed` label | Always | Operator opt-in for cross-platform work |
| PR touching macOS-sensitive paths | Always | Path filter in `determine-changes` |
| Other PRs | Skipped | Nightly catches drift within 24h |
| Daily 06:00 UTC schedule | Full macOS matrix | `ci-macos-nightly.yml` |

### macOS-sensitive paths

The planner job `determine-changes` in `ci.yml` sets
`macos_sensitive=true` when any of these paths is touched:

- `src/bernstein/core/tunnels/**`
- `src/bernstein/core/daemon/**`
- `src/bernstein/core/config/platform_compat.py`
- `src/bernstein/core/security/vault/**`
- `src/bernstein/core/security/resource_limits.py`
- `src/bernstein/core/persistence/runtime_state.py`
- `src/bernstein/core/communication/notifications.py`
- `src/bernstein/core/preview/**`
- `src/bernstein/tui/clipboard.py`
- `src/bernstein/cli/display/splash_screen.py`
- `src/bernstein/bridges/openclaw_gateway.py`
- `tests/integration/test_adapter_e2e.py`
- `scripts/run_tests.py`
- `.github/workflows/ci.yml`
- `.github/workflows/ci-macos-nightly.yml`

Keep this list in sync with the classifier in `determine-changes` and
the `push` path filter in `ci-macos-nightly.yml`. The two are
deliberately duplicated so the nightly remains self-contained.

### Operator levers

| Need | Action |
|------|--------|
| Force macOS on a specific PR | Add the `macos-needed` label |
| Force macOS for the whole repo temporarily | Set the label on every open PR, or revert this gate |
| Run macOS on demand | `gh workflow run ci-macos-nightly.yml` |
| Investigate macOS drift | Check open issues with label `ci-macos-nightly` |

### Failure handling

A failed scheduled run of `ci-macos-nightly.yml` opens (or comments
on) a tracking issue labelled `ci-macos-nightly`. The issue is
re-used while the break persists; close it after the fix lands.

Manual dispatch and push-event runs do NOT open issues, to keep the
operator-driven feedback loop quiet.

## Concurrency policy

Per-PR runs share a group keyed by PR number, `cancel-in-progress`
on. New pushes to a PR cancel older runs.

Push-to-main runs share a group keyed by branch, also
`cancel-in-progress` on. A wave of rapid merges supersedes earlier
runs so the macOS pool does not hold a queue.

Background: see issue #1273 for the wave-merge race and the
PR-vs-push split.

## Per-PR meta lanes

Every PR event used to fan out one single-step workflow run per meta
check, each spending most of its wall time on checkout + bootstrap
while holding a runner slot. These are consolidated into two
workflows so the shared runner pool serves the test matrix first:

| Lane | Workflow | Jobs | Contains |
|------|----------|------|----------|
| Policy | `pr-policy.yml` | 1 | text hygiene, main-red-guard (advisory), trunk andon gate, pre-merge autosync |
| Labels | `pr-labels.yml` (`pull_request_target`) | 1 | area labels (`actions/labeler`), size label (`pr-size-labeler`) |
| Docs | `docs-drift.yml` | 1 | drift check + data-freshness check (folded into one job) |

Step-level gating inside `pr-policy.yml` preserves the original
per-check semantics (bot-author skips, `skip-text-hygiene` /
`skip-autosync` labels, same-repo-only autosync). None of these
checks is required by branch protection; the required contexts remain
`CI gate` and `review-bot-ack` only.

Advisory scanners that duplicate other signal do not run per PR:
the vulture / refurb / perflint jobs in
`static-analysis-extended.yml` run on the weekly schedule only, and
the refurb SARIF upload is filtered to error-level results so style
findings stay out of the code-scanning alert feed.

## Required check

Branch protection points at a single status check, `CI gate`, which
rolls up `needs.*.result` for all upstream jobs and applies
intentional-skip allow-lists. The aggregator understands:

- `docs_only` skips for content-only changes
- `PR_ONLY` / `PUSH_ONLY` event-gated jobs
- `MACOS_GATED` jobs that legitimately skip on non-macOS-sensitive PRs

If you add a new conditionally-gated job, register it in the
appropriate allow-list inside the `roll-up` step of `ci-gate`.
