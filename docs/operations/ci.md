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
| `install-smoke-rpm` gating | Path-gated (#3947); skips diffs it cannot regress | `.github/workflows/ci.yml` |
| RPM smoke safety net | Daily, regardless of diff | `.github/workflows/install-smoke-rpm-nightly.yml` |
| macOS safety net | Nightly + push-on-sensitive | `.github/workflows/ci-macos-nightly.yml` |
| Required check | Single `CI gate` job | `.github/workflows/ci.yml` |
| Concurrency | PR-scoped cancel, push-scoped non-cancel | `.github/workflows/ci.yml` |
| Integration suite | Whole directory, every event, via `integration-tests` | `.github/workflows/ci.yml` |
| Collection completeness | Guard test, fails on an uncollected test file | `scripts/check_test_collection.py` |
| Feature-matrix drift | Advisory; fails on a registered command with no matrix row | `.github/workflows/feature-matrix-drift.yml` |
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
Around a dozen files under `tests/integration/` are gated behind
credentials or SDKs a hosted runner does not have (`E2B_API_KEY`,
`OPENAI_API_KEY`, `BERNSTEIN_TEST_API_KEY`, the object-store sinks, the
opt-in `cluster_e2e` marker). Those files execute no assertions here.

`scripts/run_tests.py` reports them as `NO TESTS` rather than `PASS`, and
totals them separately (`Files: P passed, F failed, N ran no tests, T
total`), so the passing count is a count of files that actually executed
something. Each file in that bucket is then named on its own line
(`  ran no tests: tests/...`): the per-file lines are printed only for
failures, so on a green shard the totals are the whole record, and a bare
count of several hundred files leaves no way to check which file executed
nothing. A file that ran nothing is not a failure - a credential-gated
suite and an empty impact-based selection are both legitimate - but it is
not evidence either. A file that exits 0 without a pytest terminal summary
*is* a failure: pytest prints one on every completed run, so its absence
means the subprocess stopped being pytest before it could report.

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

### install-smoke-rpm path gating

`install-smoke-rpm` installs the newest *released* PyPI version, not
this tree's source - it validates the RPM spec, the SRPM renderer, and
the smoke harness against whatever version is currently on PyPI. An
ordinary `src/` or `tests/` change cannot regress anything it checks,
so before #3947 the job still ran (and could red-gate the PR) on every
such diff. A defect in a past release turned every subsequent PR and
merge-queue batch red until a new release shipped, for reasons
unrelated to the change under review.

The planner (`determine-changes`) now sets `rpm_relevant_changed=true`
only when the diff touches:

- `packaging/rpm/**` (the spec and any other RPM packaging input)
- `scripts/rpm_install_smoke.sh` (the smoke harness itself)
- `.github/workflows/ci.yml` (the job's own definition)

`install-smoke-rpm` skips otherwise, and the `ci-gate` roll-up's
`RPM_SMOKE_SKIPPABLE` tolerance treats that skip as a pass - unlike the
macOS gate this has no event-shape branch: the same relevance test
applies on `push`, `pull_request`, and `merge_group` alike.

Dropping per-merge coverage on unrelated diffs would reopen the gap
the job was built to close (a broken release going undetected for
months - see the job's own header comment in `ci.yml`), so
`install-smoke-rpm-nightly.yml` re-runs the identical check daily
against whatever is currently on PyPI, regardless of what changed in
the tree. A failed nightly run opens or updates a tracking issue
labelled `ci-install-smoke-rpm-nightly`, mirroring the macOS nightly's
failure handling.

`tests/unit/test_required_check_canary_workflow_yaml.py` pins the
gating condition and the roll-up tolerance, so re-widening the job
back onto the every-merge path has to be a deliberate edit.

### Python 3.14

`nightly-deep-tests.yml` carries the only lane that executes tests on
3.14 (`unit-python-314`, the unit suite in 4 shards). Everywhere else a
3.14 pin appears it installs the package without running a test: the
`install-smoke-uv` cell in `ci.yml`, plus the auto-heal, reconcile-release
and adapter-contract-drift workflows.

It is deliberately not on the PR matrix. The `test` matrix fans out over
os x python x shard, so a python row costs 8 more jobs on every pull
request. Measured on one head SHA, `ci.yml` already puts 39 jobs on a
pull request and 47 on a push to main, out of 67 check-runs on the head,
against the 20-concurrent-job ceiling below. Buying 3.14 coverage on the
PR path would lengthen every PR's queue for an interpreter nothing ships
on yet; nightly costs 4 jobs a day and surfaces a 3.14-only regression
within 24 hours.

`tests/unit/test_nightly_deep_tests_workflow_yaml.py` pins the lane, so
dropping 3.14 coverage has to be a deliberate edit rather than an
omission.

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
checks is required by branch protection; the required context remains
`CI gate` only.

Advisory scanners that duplicate other signal do not run per PR:
the vulture / refurb / perflint jobs in
`static-analysis-extended.yml` run on the weekly schedule only, and
the refurb SARIF upload is filtered to error-level results so style
findings stay out of the code-scanning alert feed.

## Gating vs advisory workflows

One context gates a merge. Everything else is advisory and cannot
block or unblock it.

| Role | Workflow | Context published |
|------|----------|-------------------|
| Gating | `ci.yml` | `CI gate` |
| Gating | `ci-gate-stub.yml` | `CI gate` (fully-ignored diffs only) |

Everything else that triggers on `pull_request` is advisory:
`a2a-federation-e2e`, `airgap-e2e`, `bernstein-pr-review`,
`cluster-e2e`, `codeql`,
`contract-drift-autofix`, `dependabot-auto-merge`,
`dependency-review`, `docs-drift`, `feature-matrix-drift`,
`license-compliance`, `pr-labels`,
`pr-observability-summary`, `pr-policy`, `required-check-canary`,
`spa-bundle-freshness`, `spiffe-extra-e2e`, `trufflehog`, `typecheck-ts`,
`zizmor`.

### Fork pull requests get a read-only token

`permissions:` is a ceiling, not a grant. On a `pull_request` event raised
from a fork, GitHub caps `GITHUB_TOKEN` at read for **every** scope, whatever
the workflow or job asks for. A write call made anyway fails with
`Resource not accessible by integration`.

This matters for any lane whose only output is a write:

| Lane output | Same-repo PR | Fork PR |
|-------------|--------------|---------|
| Push to the PR head ref | works | 403 |
| PR comment | works | 403 |
| Tracking issue | works | 403 |

The failure mode is worse than doing nothing: the permission error becomes
the visible reason the check is red, so the contributor reads a token scope
they cannot change instead of the problem they can fix.

`contract-drift-autofix.yml` is the worked example. Same-repo PRs keep all
three write paths. Fork PRs skip them and take a report-only path instead:
the captured `[regen] ...` diagnostics (plus the patch, when regen produced
one) are written to the job summary and the step fails on those. The
workflow is advisory, so the red check does not block the merge - it just
says what to fix. `tests/unit/test_contract_drift_autofix_workflow_yaml.py`
replays the step guards for both fork states and runs the reporting script
under the runner's shell, so neither the routing nor the message can
regress silently.

A lane that needs to write on a fork PR has to move the write off the
`pull_request` event entirely (`workflow_run`, or a separate
`pull_request_target` job with a reviewed trigger) rather than widen
`permissions:`, which does nothing here.

### Pull requests opened by automation

A pull request created with the Actions token (`secrets.GITHUB_TOKEN`, or
the identical `github.token`) does not trigger workflows. Neither gating
context above ever reports on it, so branch protection holds it at
`BLOCKED` while the status rollup reads `SUCCESS`. Nothing is red and
nothing is pending; the only way out is an operator closing it, and a
closed pull request cannot be revived by force-pushing its branch, so the
next fire opens a fresh one.

Every workflow that opens a pull request therefore prefers a configured
PAT and falls back to the Actions token:

```yaml
token: ${{ secrets.BERNSTEIN_AUTOSYNC_TOKEN || secrets.GITHUB_TOKEN }}
```

The same token is used for the branch push in those lanes, because a push
made with the Actions token emits no `pull_request: synchronize` either,
which would leave a re-pushed branch showing stale checks. A lane that
opened correctly and then re-pushed with the Actions token would be worse
off than the original bug: the rollup would read green against a
superseded commit rather than reading empty. Two shapes carry the token
to git, and both are accepted:

| Shape | Lanes | How git gets the token |
|-------|-------|------------------------|
| Persisted checkout credential | `adapter-conformance-canary`, `bernstein-ci-fix`, `nightly-drift-sweep` | `actions/checkout` is given the same `token:` expression and leaves it in `.git/config` |
| Explicit auth header | `auto-heal`, `bernstein-issues-decompose` | the step checks out with `persist-credentials: false`, then sets `http.https://github.com/.extraheader` from `$GH_TOKEN` and unsets it on exit |

Passing `persist-credentials: false` without setting the header leaves the
push with no credential at all, which fails loudly rather than silently,
so it is treated as a failure by the same guard.

**What degrades without the secret.** On a fork, or in any environment
where `BERNSTEIN_AUTOSYNC_TOKEN` is unset, the expression falls through to
the Actions token and the old behaviour returns: the pull request opens
but collects no checks and cannot merge until an operator closes and
reopens it, or pushes a commit under their own identity.
`auto-heal.yml` is the exception - it detects the missing secret and
dispatches `ci.yml` on the heal branch instead, so the head SHA still
gets a `CI gate`; that dispatch is skipped when the secret is present, to
avoid a duplicate full-matrix run.

`tests/unit/test_bot_pull_request_tokens_yaml.py` discovers the
pull-request-opening steps rather than reading them from a list, so a new
automation lane cannot reintroduce the Actions token without failing the
unit suite. It sweeps `.github/workflows/*.yml` **and** the composite
actions under `.github/actions/*/action.yml`, and recognises four ways to
open a pull request: `peter-evans/create-pull-request`, `gh pr create`,
`gh api -X POST .../pulls`, and an `actions/github-script` step calling
`pulls.create`. The last two match nothing today; they are covered because
a lane that reaches for the API instead of the porcelain is the cheapest
way to reintroduce the bug past a guard that only knows two spellings.
The same module resolves the credential each lane pushes its branch with
and holds it to the same standard, and asserts the discovered set of lanes
exactly, so a widened matcher cannot quietly demand a PAT of a lane that
only reads pull requests.

### What the pool actually spends

The free-tier public-repo ceiling is 20 concurrent jobs, so the budget
is jobs, not runs. Measured on one head SHA of a workflow-touching PR
(#3157), 14 workflow runs resolved to 43 runner jobs:

| Role | Runs | Runner jobs | Share |
|------|------|-------------|-------|
| Gating (`ci.yml`) | 1 | 34 | 79% |
| Advisory (all of it) | 13 | 9 | 21% |

Counting runs instead of jobs inverts that picture and makes advisory
work look dominant. It is not: an advisory workflow is one job, `ci.yml`
is 34. Moving every advisory lane off the PR path would return under a
fifth of the pool while deleting all pre-merge signal that is not the
test matrix, so the advisory lanes stay where they are. The load that
saturates the pool during a merge wave is concurrent `ci.yml` runs, and
the durable fix for that is the merge queue (see *Concurrency policy*).

Advisory lanes are kept cheap instead of removed:
`pr-observability-summary` runs only on PRs labelled `deep-review`,
`dependabot-auto-merge` gates its only job on the Dependabot user id,
`pr-policy` and `pr-labels` are consolidated single-job lanes, and the
vulture / refurb / perflint jobs run weekly rather than per PR.

### Concurrency on `pull_request` workflows

Every workflow triggering on `pull_request` declares a `concurrency`
group keyed on the PR, and every one of them cancels a superseded
pull-request run. There are no exceptions.

`ci.yml` and `codeql.yml` express cancellation as
`cancel-in-progress: ${{ github.event_name == 'pull_request' }}`: they
cancel on the PR lane and keep the per-SHA push-to-`main` lane alive, so
a release commit's CI is never cancelled by the next merge.

`tests/unit/test_pull_request_workflow_concurrency_yaml.py` pins both
the rule and the exception list, so a new `pull_request` workflow
cannot land without a concurrency group and an exception cannot be
added silently.

#### Publishing a required context independent of a job's fate

A job publishes a check-run named after itself, so that check-run
inherits the job's fate: branch protection folds every check-run of a
required name into its verdict, and a later success does not clear an
earlier non-success. A cancelled required job then holds the commit at
BLOCKED for its whole life, and a skipped one counts as passing without
running (#3042, #3154).

When a required context must survive those states,
`scripts/publish_required_check.py` decouples it from any job's fate: it
upserts a single terminal check-run per head SHA, patching any existing
instance in place so a commit never accumulates two contradictory
verdicts. Its conclusion set is closed to `success` and `failure` -
`cancelled` is unrecoverable and `skipped`/`neutral` read as passing, so
neither is writable - and the publish step is guarded by
`if: ${{ !cancelled() }}`, so a cancelled job writes nothing and the
context stays absent (which reads as BLOCKED) until a run reports for
real. The publisher's logic is covered by
`tests/unit/test_publish_required_check.py`.

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

### Workflow-guard selection

A workflow-only pull request runs the affected slice, so the guards that
parse workflow YAML have to be inside it. Selecting those guards by the
substring `workflow` in the test file name only finds the ones that
follow that convention: a guard named after the workflow it pins reads
the same YAML, breaks on the same edit, and was left out, so the failure
surfaced on `main` after the merge instead of on the pull request.

`_workflow_test_files` unions two rules, and both are load-bearing:

| Rule | Why it cannot be dropped |
|---|---|
| `workflow` in the test file name | Keeps guards that reach workflows through a script they shell out to and so spell no workflow path of their own, `test_workflow_topology_report.py` among them |
| Test text reaches into `.github/workflows` | Keeps guards named after the workflow they pin, `test_post_ci_dispatcher_yaml.py` among them |

The content rule matches the directory written as a posix path and
assembled from path segments (`Path(".github") / "workflows"`), which is
how every guard in the tree reaches it. Measured against the tree at the
time the rule landed, the name rule alone selected 36 files and the union
selects 56.

Selection stays keyed on what a test reads. A test that merely names a
script which itself scans workflows is deliberately not selected:
`run_tests.py` holds a `.github/workflows/` constant of its own, so that
rule pulled in 18 further files that guard nothing about workflows.

```bash
uv run python scripts/test_impact.py --files .github/workflows/ci.yml --print-paths
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
