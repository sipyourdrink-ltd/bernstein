# CI runbook

Operator-facing notes on Bernstein's CI workflows. Focused on the
matrix policy and the failure-class interventions; for the per-step
documentation read the inline comments in `.github/workflows/ci.yml`.

## TL;DR

| Topic | Status | Where |
|-------|--------|-------|
| Per-PR macOS matrix | Gated (#1468) | `.github/workflows/ci.yml` |
| Per-PR Python matrix | 3.13 only; 3.12 on push | `.github/workflows/ci.yml` |
| Per-PR install smoke | 1 pipx + 1 uv cell; full 6 on push | `.github/workflows/ci.yml` |
| macOS safety net | Nightly + push-on-sensitive | `.github/workflows/ci-macos-nightly.yml` |
| Required check | Single `CI gate` job | `.github/workflows/ci.yml` |
| Concurrency | PR-scoped cancel, push-scoped non-cancel | `.github/workflows/ci.yml` |
| Collection completeness | Guard test, fails on an uncollected test file | `scripts/check_test_collection.py` |
| Required-context presence | Operator command + advisory PR step | `scripts/check_required_contexts.py` |
| Type-check scope | Blocking vs advisory scopes | `docs/operations/type-check-scope.md` |

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

## Python and install-smoke matrix policy

The `test`, `install-smoke-pipx`, and `install-smoke-uv` matrices are
event-conditional (via `fromJSON` expressions on `github.event_name`):

| Job | PR lane | push / merge_group / dispatch |
|-----|---------|-------------------------------|
| `test` | ubuntu + windows, Python 3.13, 4 shards each (8 jobs) | full matrix incl. the ubuntu 3.12 row (12 jobs) |
| `install-smoke-pipx` | ubuntu / 3.13 (1 cell) | ubuntu + macos x 3.12 + 3.13 (4 cells) |
| `install-smoke-uv` | ubuntu (1 cell) | ubuntu + macos (2 cells) |

Rationale: PR pushes are the high-frequency event on the shared
runner pool, and the slimmed rows re-run on every push to main, so a
row-specific regression (a 3.12-only failure, a macOS packaging
break) surfaces at most one merge later and is attributable to a
single commit. `ci-macos-nightly.yml` and `nightly-deep-tests.yml`
remain the scheduled safety nets.

The CI gate aggregation is unchanged: `ci-gate` still rolls up
`needs.*.result` for every job (all remaining matrix cells included)
and still reports on `pull_request` and `merge_group`.

## Concurrency policy

| Event | Group key | `cancel-in-progress` |
|-------|-----------|----------------------|
| `pull_request` | PR number | true |
| push to `main`, `merge_group`, `workflow_dispatch` | branch + `github.sha` | false |

Per-PR runs share a group keyed by PR number, stable across pushes
to the same PR. A new commit cancels the older run, so reviewers
only ever wait on the latest push and we don't burn minutes on
stale SHAs.

Push-to-main runs are keyed per-SHA and never cancel. Every commit
that lands on main runs its own full-matrix CI to completion, so
the commit history carries a real per-commit pass/fail signal
instead of a run of "cancelled" markers left behind when a burst of
merges supersedes each other. A cancelled run on an already-merged
commit reads as red forever and hides genuine failures behind
noise; keying main by SHA removes that class of false red.

Tradeoff: a rapid merge wave now keeps N full main runs alive
instead of one. The branch-scoped policy this replaces was chosen
after a May 2026 wave of 13 merges in 90 minutes saturated the
runner queue. The load stays bounded because main pushes are merged
PRs, far fewer than PR-branch pushes, and PR-branch pushes still
cancel, so the saturation source stays capped. The durable fix for
burst load is the merge queue: `ci.yml` already triggers on
`merge_group`, which tests each batch once on the prospective
merged SHA.

Background: see issue #1273 for the wave-merge race and the
PR-vs-push split. The rationale is restated in the comment block
above the `concurrency:` key in `.github/workflows/ci.yml`.

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

## Gate evaluation coverage

A green gate is only evidence of correctness when the gate evaluated the
work. Two guards make the difference between "everything passed" and
"nothing ran" visible.

### Collection completeness

`scripts/check_test_collection.py` walks `tests/` and reports any test
file that no CI configuration collects. The collected set is derived from
the workflows themselves, not restated:

| Source | What it contributes |
|---|---|
| `run:` bodies in `.github/workflows/*.yml` | Every `pytest` / `run_tests.py` invocation |
| `scripts/run_tests.py::DEFAULT_TEST_DIR` | The directory the shards discover when no `--test-dir` is given |
| `scripts/test_impact.py::TEST_DIRS` | The universe a `--affected` run can select from |

Rules the derivation applies:

- a `run_tests.py` directory collects `test_*.py` only (its `rglob`
  pattern), so a `*_test.py` file under a shard directory counts as
  uncollected;
- a `pytest` directory collects pytest's own `python_files` patterns;
- a `-k`-narrowed invocation credits nothing (the expression, not the
  path, decides what runs);
- a `-m`-narrowed invocation credits the path (collection still walks it).

`tests/unit/scripts/test_check_test_collection.py` runs the derivation in
the shards, so adding a test file somewhere no shard reaches fails CI.
A file that is deliberately not run in CI needs an entry, with a reason,
in that script's `ALLOWLIST`; an entry that stops matching a file is
reported as stale and must be removed.

```bash
uv run python scripts/check_test_collection.py          # report
uv run python scripts/check_test_collection.py --json   # machine-readable
```

### Required-context presence

A head commit that never produced a check-run for a required context
shows the same empty failure list as a commit whose checks all passed.
`scripts/check_required_contexts.py` names which of the two a commit is
in. The required contexts are read from
`.github/workflows/required-check-canary.yml`
(`BRANCH_PROTECTION_CONTEXTS_JSON`) - the same in-tree source the
scheduled branch-protection audit compares live settings against. The
script reads check-runs only; it never touches branch protection.

```bash
uv run python scripts/check_required_contexts.py --pr 1234
uv run python scripts/check_required_contexts.py --sha "$GITHUB_SHA" --json
```

| State | Meaning |
|---|---|
| `missing` | No check-run with that name on the commit - the context never ran |
| `pending` | Present, not finished |
| `failing` | Completed as failure / timed_out / cancelled / action_required |
| `skipped` | Completed as skipped |
| `passing` | Completed as success / neutral |

Exit status is non-zero only for `missing`; a red required check is the
merge gate's business, an absent one is this script's. The same command
runs as an advisory step in `pr-observability-summary.yml` (opt-in via
the `deep-review` label or `workflow_dispatch`) and reports into the job
summary without gating.

Reach for it when a PR reads BLOCKED with no visible failures: either a
required context is `missing`, or it is present and `failing`.

### Type-check scope

Which paths each type job covers, and what the `|| true` runs report,
are documented in `docs/operations/type-check-scope.md`.
