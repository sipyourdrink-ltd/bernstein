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
| Integration suite | Whole directory, every event, via `integration-tests` | `.github/workflows/ci.yml` |
| Collection completeness | Guard test, fails on an uncollected test file | `scripts/check_test_collection.py` |
| Required-context presence | Operator command + advisory PR step | `scripts/check_required_contexts.py` |
| Type-check scope | Blocking vs advisory scopes | `docs/operations/type-check-scope.md` |

## Test directory coverage

Every test file must be reachable from a CI lane. The mapping:

| Directory | Lane | Events | Selection |
|---|---|---|---|
| `tests/unit/**` | `test` (4 shards x os x python) | pull_request | impacted slice only (`--affected`) |
| `tests/unit/**` | `test` (4 shards x os x python) | push, merge_group, workflow_dispatch | whole directory |
| `tests/integration/**` | `test` (4 shards x os x python) | pull_request | impacted slice only (`--affected`) |
| `tests/integration/**` | `integration-tests` | all | whole directory |
| `tests/property/**` | `property-tests` | all | whole directory |
| `tests/snapshot/**` | `snapshot-tests` | all | whole directory |
| `tests/contract/**` | `schemathesis-smoke` | all | whole directory |
| `tests/protocol/**` | `publish.yml` | release | whole directory |
| `tests/pentest/**` | `pentest.yml` | scheduled / dispatch | whole directory |
| `tests/stress/**` | `nightly-deep-tests.yml` | nightly | whole directory |
| `tests/chaos/**` | none - on demand | operator | not run in CI |
| `tests/perf/**` | none - wall-clock thresholds are not meaningful on shared runners | operator | not run in CI |

Two things this table is deliberately explicit about:

- On `pull_request` the `test` job runs `scripts/run_tests.py --affected`,
  which selects only the files the impact map ties to the changed sources.
  The whole `tests/unit/**` directory runs on push, in the merge queue and
  on manual dispatch, not on a PR. A file that no lane other than the
  affected slice covers is therefore not guaranteed to run before a merge.
- `tests/chaos/**` (11 files), `tests/perf/**` (1 file) and
  `tests/test_worktree.py` are collected by no lane at all. `tests/protocol`,
  `tests/pentest` and `tests/stress` do run, but in workflows that do not
  feed the required `CI gate` context, so they cannot block a merge either.

A test file that lives outside all of these directories is collected by
nothing. Add new test files under one of the directories above.

### Why `integration-tests` runs on pull_request too

The `--affected` slice selects only the integration files the impact map
ties to the changed sources. A break that arrives through a path the map
does not model - a changed default, a role template, a transitive import
- was invisible to every lane that decides what reaches `main`. Only two
integration files ran on push (`test_capability_matrix_spawn_refusal.py`
and `test_adapter_e2e.py`, both pinned by name); the other 124 did not.

Restricting the job to push and `merge_group` was considered and rejected.
The `main-merge-queue` ruleset is currently disabled, so `merge_group`
never fires; a push-only job reports on `main` after a merge instead of
gating it, and the required `CI gate` context on a PR would still report
success having never run the directory. The impact map's blind spots are
the same on a PR as on a push, so the job runs on every event and a skip
is tolerated on none of them.

The measured cost of running the whole directory is 264s wall at
`--parallel 4` (126 files, 456 MB peak RSS). It runs concurrently with the
`test` shards, whose timeout is 90 minutes, so it does not extend the
critical path.

Note that a file-level pass is not the same as a directory-level pass.
`scripts/run_tests.py` reports a file whose tests all skip as `PASS`, and
around a dozen files under `tests/integration/` are gated behind
credentials or SDKs a hosted runner does not have (`E2B_API_KEY`,
`OPENAI_API_KEY`, `BERNSTEIN_TEST_API_KEY`, the object-store sinks, the
opt-in `cluster_e2e` marker). Those files execute no assertions here.

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

### The two emitters of `CI gate`

`ci.yml` is `paths-ignore`-filtered, so a pull request whose diff is
entirely inside that list never triggers it and never publishes the
required context. `ci-gate-stub.yml` exists to publish a synthetic
success for exactly those pull requests.

`paths` and `paths-ignore` are evaluated per file with OR semantics: a
workflow fires when *at least one* changed file matches. On a mixed diff
both workflows therefore fire, and for a while both published `CI gate`.
The stub finished in seconds; the real matrix was still queued. PR #3016
merged that way, with no test run against its code.

The stub now derives a verdict in-job (`scripts/ci_gate_stub_guard.py`,
which reads `ci.yml`'s own `paths-ignore` list) and takes its check-run
name from it:

| verdict | check-run name | effect |
| --- | --- | --- |
| every changed path ignored | `CI gate` | unblocks the PR, real CI will never report |
| any changed path not ignored | `CI gate stub (not applicable)` | cannot satisfy branch protection |

Two rules follow for anyone editing that workflow:

- Do not gate the emitting job with `if:`. GitHub counts a **skipped**
  required check as passing, so an `if:` skip looks like a fix and is
  not one. It also posts the unresolved `name:` template as the
  check-run name when the job is skipped.
- Do not give the stub a second, unconditional `CI gate` job. The
  required-check canary rejects any emitter outside the two allow-listed
  ones, including one hidden behind a name template.

### Reading the required check is not the same as reading CI

A rerun **resets** the check-run of the job it reruns. After a rerun the
newest instance of `CI gate` on a head SHA can be a stale success from an
earlier attempt while the real run is still in flight. A probe that reads
"the latest instance of the required context" will report ready on a pull
request whose tests are unfinished or failing.

When scripting a readiness check against a head SHA:

- Enumerate **every** check run named `CI gate` on that SHA, not the
  newest one. Treat any instance with `status != completed` as not ready.
- Require `status == completed` **and** `conclusion == success`. A
  `queued` run has no conclusion, which is easy to read as "not failing".
- Confirm the run that produced the success is the real `CI` workflow
  when the diff contains a non-ignored path. The stub's own check is
  named `CI gate stub (not applicable)` in that case, so a `CI gate`
  success there must have come from `ci.yml`.

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
