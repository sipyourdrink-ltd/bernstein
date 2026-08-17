# Merge queue runbook

Operator-facing notes on the GitHub merge queue for `main`. The queue is
configured by a repository **ruleset** (not legacy branch protection).

**This document is the source of truth for the queue configuration.** The
live ruleset matches it (`scripts/verify_merge_queue_ruleset.py` exits `0`
as of 2026-08-14); [Enable](#enable-mechanical-steps) records the calls
that applied and activated it.

## TL;DR

| Topic | Status | Where |
|-------|--------|-------|
| Queue state today | `enforcement: active` since 2026-08-14 | ruleset `main-merge-queue` |
| Flip status | Flipped 2026-08-14 - both blockers cleared by re-measurement | [Blockers](#blockers-to-the-flip) |
| What it solves | Tests the A+B *combination* before merge | this doc |
| Required checks on the queue | `CI gate` | `ci.yml` |
| What the group's CI is planned against | The group's `base_sha`, never `HEAD~1` | [What the group's CI actually tests](#what-the-groups-ci-actually-tests) |
| Merge method | Squash | ruleset `merge_queue` rule |
| Grouping | `ALLGREEN` (all entries in a group must pass) | ruleset |
| Batch size | **1** — one PR lands per push to `main` | [Tunables](#tunables-source-of-truth) |
| Auto-release | Unaffected; the post-queue push to `main` fires the listener | [Auto-release](#auto-release-through-the-queue) |
| Enable | Two `gh api PUT` calls, in order | [Enable](#enable-mechanical-steps) |
| Pause | Set ruleset `enforcement` to `disabled` | below |
| Rollback | Delete the ruleset | below |

## Why a merge queue

Branch protection on `main` has `required_status_checks.strict = false`,
so a PR is **not** required to be up to date with `main` before merging.
Two PRs can both branch from `main@X`, both pass `CI gate` against base
`X`, and both auto-merge - but the **combination** of the two is never
built or tested. That is how a red `main` lands despite two green PRs
(observed: a test fixture and a workflow change merged in separate green
PRs were red once combined).

The merge queue closes that gap. Each candidate is tested **on top of the
other queued candidates** on a synthetic `merge_group` ref before it is
allowed to merge. With `ALLGREEN` grouping, a batch only merges if the
whole batch is green; a red entry is ejected and the rest re-form.

## How the queue gates

```
PR ready ->  enters queue  ->  CI runs on merge_group ref  ->  required checks green?
              (passes PR-                (the prospective            |
               level required             merged SHA)                yes -> merge (squash)
               checks first)                                         no  -> eject, re-form group
```

Two distinct gates, do not conflate them:

| Gate | Checks enforced | Trigger event |
|------|-----------------|---------------|
| **Enter the queue** | Legacy branch-protection required checks (`CI gate`) | `pull_request` |
| **Merge from the queue** | Ruleset `required_status_checks` (must be `CI gate`) | `merge_group` |

`CI gate` (the `ci-gate` aggregator job in `ci.yml`) runs on `merge_group`
because the workflow declares `merge_group: {}` and `ci-gate` has
`if: always() && !cancelled()` with no event exclusion.

### Required-check coverage under `merge_group`

The single required context also reports on a `merge_group` ref:

| Required context | Emitting workflow | Runs on `merge_group`? |
|------------------|-------------------|------------------------|
| `CI gate` | `ci.yml` :: `ci-gate` | Yes - `merge_group: {}` |
| `CI gate` | `ci-gate-stub.yml` :: `ci-gate` | No - `pull_request` only, and **correct** (see below) |

`ci-gate-stub.yml` deliberately has no `merge_group` trigger. It exists
only because `ci.yml`'s `pull_request` trigger carries a `paths-ignore:`
list, so a PR whose diff is fully ignored never publishes `CI gate`.
**`paths` / `paths-ignore` filters are only evaluated for `push`,
`pull_request` and `pull_request_target`** - they do not apply to
`merge_group`. `ci.yml` therefore runs unconditionally on every merge
group, including docs-only ones, and always publishes `CI gate`. Adding
`merge_group` to the stub would publish a second check run under the same
required-context name on the queue for no benefit.

That makes `ci.yml`'s **unconditional** `merge_group: {}` trigger a
load-bearing invariant: adding any filter to it would silently wedge the
queue for every diff the filter excludes. It is locked by
`tests/unit/test_required_check_canary_workflow_yaml.py`, and the queue's
required-context coverage by
`tests/unit/test_merge_queue_gate_coverage_yaml.py`.

### Reports on the queue but is not required yet

| Context | Emitting workflow | Runs on `merge_group`? | Required? |
|---------|-------------------|------------------------|-----------|
| `shipped bundle matches the lockfile` | `spa-bundle-freshness.yml` :: `rebuild` | Yes - `merge_group: {}` | **No - see below** |
| `typecheck (sdk/typescript)` | `typecheck-ts.yml` :: `typecheck` | Yes - `merge_group: {}` | **No - see below** |
| `typecheck (packages/vscode)` | `typecheck-ts.yml` :: `typecheck` | Yes - `merge_group: {}` | **No - see below** |
| `typecheck (web)` | `typecheck-ts.yml` :: `typecheck` | Yes - `merge_group: {}` | **No - see below** |
| `typecheck (templates/cloudflare-mcp-server)` | `typecheck-ts.yml` :: `typecheck` | Yes - `merge_group: {}` | **No - see below** |

`typecheck-ts` occupies four rows because it publishes four contexts: its
job is `typecheck (${{ matrix.package }})` and branch protection matches a
context by its exact rendered string, so requiring "the typecheck lane"
means listing every cell. The matrix is **derived from the tree** -
`test_every_typescript_package_is_in_the_matrix` fails if a package that
declares a `typescript` dependency is missing from it - so this list is not
a constant. Adding a TypeScript package adds a context nobody required (a
hole); deleting or renaming one strips a required context of its emitter (a
wedge). Neither reports as a red check anywhere, so
`test_queue_reporting_lane_contexts_are_named_in_the_runbook` fails when the
matrix and this table stop agreeing.

#4010 merged with this lane red. `CI gate` was green, `CI gate` is the only
required context, so branch protection was satisfied and the queue took the
entry; `main` went red and the repair landed at the back of an eleven-entry
queue. The lane could only ever annotate the damage, never prevent it.

#4028 added the trigger, which is the half that has to come first. The lane
now reports on a queued ref, so the remaining step is safe to take and is
**two flips, both maintainer-only**:

1. Branch protection on `main`: add `shipped bundle matches the lockfile` to
   the required-status-checks list. This is what stops the pull request
   entering the queue.
2. The merge-queue ruleset: add the same context to
   `required_status_checks`, in the live ruleset (the `gh api PUT` payload
   above) **and** in its checked-in mirror
   `docs/operations/merge-queue-ruleset.json`, which
   `tests/unit/test_merge_queue_gate_coverage_yaml.py` reads.

Do them in that order, and only while the queue is drained: editing required
contexts invalidates every in-flight entry, so each one restarts a full-suite
run. Until both are done the lane is exactly as advisory as it was before -
the trigger alone changes nothing about whether a red bundle can merge.

#4073 puts `typecheck-ts` in the same state for the same reason, and the two
flips are the same two flips - with all four contexts from the table above,
not one. Two things are worth doing before throwing that switch, neither of
which applies to the bundle gate:

- **Wait for the lane to have reported on a real queued ref at least once.**
  Nothing here has ever run on a `merge_group` event, and a context that
  turns out not to arrive wedges the queue rather than blocking the pull
  request.
- **Require all four cells or none.** Requiring a subset leaves the
  unrequired packages in exactly the state #4073 describes, while costing
  the full four-slot run anyway.

Cost, measured over the 14 most recent runs (56 cells) before #4073, with
queue wait separated from execution: **execution 16s min, 28s median, 45s
max per cell**; the cells run in parallel, so a run finishes in a **38s
median, 45s max**, at **four concurrent slots**. Queue wait is a median of
3s but reaches **933s**, and that is the number to distrust in the run list:
the 16-minute run `31953555527` was 933s of waiting for a runner wrapped
around a 43s job. #4020's rule - a lane that cannot gate a merge should not
compete for runners with the one that can - is not in tension with either
trigger for long, because both lanes are on their way to being required,
which is the case that rule excludes. `typecheck-ts` is nonetheless four
times the runner cost of the bundle gate, and that is the honest reason to
finish its flip rather than leave the trigger sitting there indefinitely.

The general rule this came from is locked by
`test_no_web_triggerable_lane_is_advisory_only`: a lane that can fail on a
`web/**` change is either part of the required gate, on its way to being
required, or a written-down deferral. The state that produced #4010 - a lane
whose reason for staying advisory lived in a comment, with nothing failing
when that reason expired - is the one that is no longer reachable.

### What the group's CI actually tests

Reporting `CI gate` on the group is necessary but not sufficient: the gate
has to be green for the *right* reason. `ci.yml` classifies the diff first
(`determine-changes` emits `docs_only` / `macos_sensitive`) and the roll-up
tolerates a skipped job only when that classification says the skip was
intentional. So the classification is what decides how much of the suite a
queued group really runs.

On a `merge_group` event the ref is
`gh-readonly-queue/main/pr-<n>-<base_sha>`, which stacks every entry in the
group on top of `main`. The planner therefore diffs against
`github.event.merge_group.base_sha` - the group's own base - and sees the
whole combined change.

The push heuristic (`HEAD~1...HEAD`) must not be reused here. It reads only
the tail commit, so a group whose last commit happens to be docs-only would
classify as `docs_only=true`, every job in the roll-up's `DOCS_ONLY_SKIPPABLE`
set would skip, and `CI gate` would go green for a combination nothing built -
reintroducing the untested-combination hole the queue exists to close. If the
base SHA is absent or unresolvable the planner falls back to the over-broad
"everything changed" classification, so an infrastructure problem costs runner
minutes rather than coverage.

Locked by `tests/unit/test_required_check_canary_workflow_yaml.py`, which runs
the shipped classifier against a synthetic two-entry group whose tail entry is
docs-only.

## macOS coverage under the queue

The macOS matrix (`test-macos`, `adapter-integration-macos`) is **gated**:
it runs on `push` to `main`, on macOS-sensitive diffs, and on the
`macos-needed` label. On a queued group the label and `push` branches cannot
fire, so `macos_sensitive` - computed from the group's combined diff - is what
decides: a group touching a macOS-sensitive path runs the macOS cells in the
queue, and a group that does not skips them. The `CI gate` roll-up tolerates
exactly that skip (see `MACOS_SKIP_EVENTS` in `ci.yml`). Coverage is preserved
because the **post-merge `push` to `main`** runs the full macOS suite
un-gated, and `ci-macos-nightly.yml` is the daily safety net. The queue
validates the integrated combination; the merged commit validates macOS.

## Auto-release through the queue

`post-ci-dispatcher.yml` listens on `workflow_run` for the `CI` workflow,
filtered to `branches: [main]`, and routes to `auto-release.yml`. The
release gate then inspects **the triggering commit only** and tags when
that commit changed `version = ` in `pyproject.toml`. Two questions had to
be answered before the queue can be enabled.

**Q1: do merge-queue CI runs dispatch the release listener?**
No, and that is the desired behaviour. A `merge_group` run reports
`head_branch = gh-readonly-queue/main/pr-<n>-<base_sha>`, which the
dispatcher's `branches: [main]` filter excludes. Nothing is ever tagged
from a queue ref that has not merged.

**Q2: does the post-queue merge still fire the release listener?**
Yes. When a merge group goes green, GitHub advances the base branch and
emits an ordinary `push` event on it. The push is not made with
`GITHUB_TOKEN`, so Actions' loop prevention does not suppress it and
`push`-triggered workflows start normally.

Measured in **this** repository, during the queue window of 2026-05-22
(ruleset `main-merge-queue` was briefly `active`; `gh run list --event
merge_group` still lists the runs). Pull request #1842, `merge_method:
SQUASH`:

| Step | Evidence |
|------|----------|
| Queue built the entry | `merge_group` CI run `26261037066` / `26264196218`, `head_branch: gh-readonly-queue/main/pr-1842-75aa8698…`, conclusion `success` |
| Entry merged | `gh api .../issues/1842/timeline` -> `{"event":"merged","created_at":"2026-05-22T02:23:09Z"}` |
| `push` fired on `main` | CI run `26264756775`, `event: push`, `head_branch: main`, `head_sha: ff07c5aa…`, 02:23:12Z |
| Listener fired off it | `Post-CI dispatcher` run `26264768644`, `event: workflow_run`, `head_sha: ff07c5aa…`, conclusion `success`, 02:23:35Z |

Pull request #1839 shows the same chain 90 minutes earlier
(`4130e2445c…` -> push CI `26262116854` -> dispatcher `26262118810`).

**Two corrections to the earlier revision of this section**, both from the
run data above rather than from a third-party repository:

*The merged SHA is a new SHA.* Under `merge_method: SQUASH`, `main` does
**not** advance to the SHA the queue tested. The group's head was
`d8bdb47944…`; the commit that landed on `main` was `ff07c5aa29…`, and
their trees differ too (`a93c9b5a06…` vs `ac87197073…`). Squash rewrites
the commit onto the branch head at merge time. Only `merge_method: MERGE`
makes the tested SHA the merged SHA. Any smoke test that asserts SHA
equality will fail on a healthy queue.

*The actor is not `github-merge-queue[bot]` here.* That identity came
from a third-party observation. This repository's own queue merges
reported `actor: chernistry` on the resulting push run - the operator who
queued the entry. The property the release path depends on is narrower
than either name: the push is not made by the Actions token, so it is not
loop-suppressed.

So the chain is unchanged end to end:

```
queue merges -> push to main (same SHA) -> ci.yml (push) -> workflow_run
             -> post-ci-dispatcher (branches: [main]) -> auto-release -> tag -> publish
```

`pyproject.toml` is deliberately **not** in `ci.yml`'s `push.paths-ignore`
list, so a version-bump commit always triggers the CI run the listener
needs. That is asserted by `tests/unit/test_post_ci_dispatcher_yaml.py`.

**Both halves of that chain were observed on the last live queue window
(2026-05-21/22), not inferred.**

*A queue merge does raise a `push` run on `main`.* PR #1842 merged out of
the queue at `2026-05-22T02:23:09Z` (`merge_commit_sha ff07c5aa2`), and
`ci.yml` fired on that commit three seconds later:

```console
$ gh api ".../actions/workflows/ci.yml/runs?head_sha=ff07c5aa2938..." \
    --jq '.workflow_runs[] | "\(.created_at) event=\(.event) branch=\(.head_branch)"'
2026-05-22T02:23:12Z  event=push  branch=main
```

*A queue build does not.* Every `merge_group` CI run in that window
carried a `gh-readonly-queue/...` head branch, so none of them matches
the dispatcher's `branches: [main]` filter:

```console
$ gh run list --event merge_group --limit 10 --json headBranch
gh-readonly-queue/main/pr-1842-75aa8698...   (10 of 10 in this shape)
```

That is the property the release path needs: the queue's own CI cannot
trigger a tag from an ephemeral ref, and the post-merge push still can.
What has **not** been observed end to end is a version bump actually
tagging and publishing through a live queue - no release happened during
that window. Step 4 below is where that gets confirmed.

**The invariant this depends on: one PR per push.** The release gate keys
on the push's head SHA. If a merge group merged N > 1 entries at once, the
base branch would advance by N commits in a single push, and the push
would report only the last entry's SHA. A version bump sitting anywhere
but last in the batch would be skipped silently - green CI, no tag, no
publish, no error. `max_entries_to_merge` is pinned to `1` for this
reason; see [Tunables](#tunables-source-of-truth).

## Tunables (source of truth)

The values below are authoritative. The live ruleset was reconciled to
them on 2026-08-14 (Step 1; verifier exits `0`) - the "Shipped" column
records what the ruleset carried before that reconciliation.

The machine-readable copy is
[`merge-queue-ruleset.json`](merge-queue-ruleset.json). It is the exact
body Step 1 PUTs, and `scripts/verify_merge_queue_ruleset.py` diffs the
live ruleset against it, so "the shipped ruleset drifted from the
runbook" is now one command rather than a careful read.
`tests/unit/test_merge_queue_ruleset_spec.py` keeps the file, this table
and the Step 1 payload from becoming three different answers.

| Parameter | Shipped | Correct | Why |
|-----------|---------|---------|-----|
| `merge_method` | `SQUASH` | `SQUASH` | One commit per PR on `main` |
| `grouping_strategy` | `ALLGREEN` | `ALLGREEN` | A group merges only if every entry is green |
| `max_entries_to_build` | 1 | **5** | Build concurrency: the number of `merge_group` webhooks in flight. At `1` the queue tests one entry at a time, so a burst of N PRs costs N sequential full CI cycles - strictly worse latency than today with no load benefit the per-SHA policy does not already provide. `5` caps concurrent queue CI at 5 instead of the unbounded N a burst produces today. |
| `min_entries_to_merge` | 1 | **1** | Never wait for a second entry before merging |
| `max_entries_to_merge` | 1 | **1** | One PR lands per push to `main`. The auto-release gate keys on the push head SHA, so merging N entries in a single push silently skips a version bump that is not last in the batch - green CI, no tag, no publish, no error. Lifting this requires the release gate to stop keying on the push head SHA; tracked as a follow-up. The second former blocker is closed: `determine-changes` now classifies a queued group against `github.event.merge_group.base_sha`, so a multi-entry group is planned from its combined diff instead of its last commit. |
| `min_entries_to_merge_wait_minutes` | 0 | **0** | With `max_entries_to_merge = 1` there is no batch to fill, so any wait is pure added latency |
| `check_response_timeout_minutes` | 30 | **240** | Re-measured 2026-07-27 over the last 30 successful `CI` runs on `main` (`run_started_at` -> `updated_at`): p50 **49** min, p90 **214** min, max **243** min, and **30 of 30 exceeded 30 minutes**. The earlier figures in this row (p50 38 / p90 58 / max 105) are stale. At the shipped `30`, *every* entry is ejected as timed out. At `120`, 9 of those 30 runs would still have been ejected. `240` covers all but the single 243-minute outlier. Re-measure in Step 0 before flipping - this value tracks runner-pool contention, not the test suite. |

## Blockers to the flip

**Both cleared 2026-08-14; the queue was flipped the same day.** The
re-measurement over the last 40 concluded `CI` push runs on `main`
(`created_at` -> `updated_at`, the same window the queue timeout
measures): p50 **46** min, p90 **93** min, max **115** min, 0 of 40 over
240. Blocker 1 fell to Step 1 (timeout raised to `240`, verifier exits
`0`). Blocker 2's arithmetic was measured against the July runner-pool
contention (p90 214) *and* the shipped `max_entries_to_build: 1`; with
build concurrency `5`, a burst of up to five ready PRs is built as
stacked groups concurrently, so its wall-clock cost is one CI cycle
(p90 ~93 min), not N sequential cycles. Sequential cost returns only
when an entry ejects and the groups behind it rebuild.

The table below is the July record, kept because the timeout row's
rationale depends on it. These were conditions on the repository, not
code changes; neither was fixed by a pull request.

| # | Blocker | Evidence | Clears when |
|---|---------|----------|-------------|
| 1 | The shipped ruleset would eject **every** entry. `check_response_timeout_minutes` is `30`; every measured `CI` run on `main` took longer than that. | p50 49 min, p90 214 min, max 243 min, 30/30 over 30 min. Re-checked 2026-07-27 over the last 40 concluded runs (`created_at` -> `updated_at`, which is what the queue timeout measures): p50 110, p90 224, max 243, **0 of 40** under 30 min | Step 1 is applied (raises it to `240`) and the verifier exits `0` |
| 2 | CI wall time makes serialised merging impractical. `max_entries_to_build` is the number of groups built at once, but `max_entries_to_merge` is pinned to `1` for the release path, so a burst of N ready PRs costs N sequential merges. At p90 = 214 min that is most of a day for five PRs. | same distribution as #1 | CI wall time is bounded, or the release gate stops keying on the push head SHA so batches can merge |

That order was followed on 2026-08-14: Step 1 applied, wall time
re-measured (above), flip, and a docs-only canary through the queue with
Step 4 open. One operator-facing change follows from the flip: a direct
`PUT /pulls/{n}/merge` no longer lands a PR - merging means enqueueing,
via `gh pr merge <n> --squash` (add `--auto` to enqueue before checks
finish) or the merge button. Everything a sweep used to do after the
merge call - confirm the merge landed, fetch `main`, check its tip - is
unchanged; it just happens after the queue reports rather than after the
API call returns.

## Enable (mechanical steps)

**Do not run these while a release is in flight, or while a batch of
pull requests is being landed in a deliberate order.** Enabling the queue
serialises every open PR through it, and a queue re-forms its group after
every ejection - so a set of PRs that conflict with each other (the usual
case for several PRs touching `.github/workflows` and the regenerated
`docs/operations/ci-topology.md`) will eject and rebuild repeatedly, each
rebuild costing a full CI cycle.

All calls need repository **admin** scope.

**Step 0 - preflight (read-only).** Confirm the ruleset id and the
branch-protection contexts the queue must mirror.

```bash
REPO=sipyourdrink-ltd/bernstein

gh api "repos/$REPO/rulesets" --jq '.[] | "\(.id)\t\(.name)\t\(.enforcement)"'
gh api "repos/$REPO/branches/main/protection/required_status_checks" \
  --jq '.checks[] | "\(.context)\tapp_id=\(.app_id)"'
```

Expected: ruleset `main-merge-queue` at `enforcement: disabled`, and
exactly one context - `CI gate`, `app_id` `15368` (GitHub Actions).

Then diff the live ruleset against the spec. This is the check that
names the drift instead of asking you to spot it:

```bash
python scripts/verify_merge_queue_ruleset.py
```

Exit `0` means the ruleset is already correct and you can skip to Step 2.
Exit `1` prints one line per drifted field; as of 2026-07-27 it prints:

```console
ruleset main-merge-queue (id 16719298)
enforcement: disabled
DRIFT: 2 field(s) disagree with the spec
  - max_entries_to_build: max_entries_to_build: live 1, spec 5
  - check_response_timeout_minutes: check_response_timeout_minutes: live 30, spec 240
```

**Step 1 - correct the rules while still disabled.** This applies the
tunables above. Keep `enforcement: disabled` in this payload: a mis-typed
rule set then cannot enable the queue as a side effect.

The spec file is the payload, so the whole step is one call:

```bash
gh api -X PUT "repos/$REPO/rulesets/$RULESET_ID" \
  --input docs/operations/merge-queue-ruleset.json
python scripts/verify_merge_queue_ruleset.py   # must now exit 0
```

The identical body is inlined below for operators reading this page
without a checkout.

```bash
REPO=sipyourdrink-ltd/bernstein
RULESET_ID=16719298   # confirm with Step 0

gh api -X PUT "repos/$REPO/rulesets/$RULESET_ID" --input - <<'JSON'
{
  "name": "main-merge-queue",
  "target": "branch",
  "enforcement": "disabled",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    {
      "type": "merge_queue",
      "parameters": {
        "merge_method": "SQUASH",
        "grouping_strategy": "ALLGREEN",
        "max_entries_to_build": 5,
        "min_entries_to_merge": 1,
        "max_entries_to_merge": 1,
        "min_entries_to_merge_wait_minutes": 0,
        "check_response_timeout_minutes": 240
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": true,
        "required_status_checks": [
          { "context": "CI gate", "integration_id": 15368 }
        ]
      }
    }
  ]
}
JSON
```

Verify the write landed before continuing:

```bash
gh api "repos/$REPO/rulesets/$RULESET_ID" --jq '.rules'
```

**Step 2 - drain.** Confirm nothing is mid-merge, so no PR is caught
between the two merge paths:

```bash
gh pr list --repo "$REPO" --state open --json number,title,autoMergeRequest \
  --jq '.[] | select(.autoMergeRequest != null) | "\(.number)\t\(.title)"'
```

**Step 3 - flip.** A separate call, so enabling is one reviewable action:

```bash
gh api -X PUT "repos/$REPO/rulesets/$RULESET_ID" -f enforcement=active
```

**Step 4 - smoke test.** Enqueue one low-risk PR and confirm the whole
chain, in order:

1. A `merge_group` run appears: `gh run list --repo "$REPO" --event merge_group --limit 5`.
2. `CI gate` reports on it. If it does not report, the entry sits forever -
   pause immediately rather than waiting it out.
3. The PR merges and `main` gains a new commit. Do **not** assert it is
   the SHA the queue tested: under `merge_method: SQUASH` it is a new
   commit with a different SHA and possibly a different tree (see
   [Auto-release](#auto-release-through-the-queue)).
4. A `push` run on `main` appears at `main`'s **new** head SHA. Read the
   actor for the record, but do not gate on a particular one - the only
   requirement is that the run exists.
5. `post-ci-dispatcher.yml` runs off that CI run:
   `gh run list --repo "$REPO" --workflow post-ci-dispatcher.yml --limit 3`.

If step 4 or 5 does not happen, pause the queue immediately (below) - the
release path is broken and every subsequent merge accumulates untagged.

**Not part of the flip.** The main-red-guard advisory was consolidated
into `.github/workflows/pr-policy.yml` and no longer exists as a
standalone workflow. It already warns rather than fails and is not a
required context, so it needs no change when the queue is enabled and
nothing has to be retired.

## Pause (keep the ruleset, stop enforcing)

Set the ruleset `enforcement` to `disabled`. PRs then merge via the
legacy branch-protection path again (no queue). Re-enable by setting
`enforcement` back to `active`.

```bash
# Find the ruleset id
gh api repos/sipyourdrink-ltd/bernstein/rulesets --jq '.[] | "\(.id)\t\(.name)"'

# Pause
gh api -X PUT repos/sipyourdrink-ltd/bernstein/rulesets/<RULESET_ID> \
  -f enforcement=disabled

# Resume
gh api -X PUT repos/sipyourdrink-ltd/bernstein/rulesets/<RULESET_ID> \
  -f enforcement=active
```

## Rollback (remove the queue entirely)

```bash
gh api -X DELETE repos/sipyourdrink-ltd/bernstein/rulesets/<RULESET_ID>
```

After deletion, `main` reverts to the legacy branch-protection required
checks. Any PRs sitting in the queue are released back to normal merge.

## Troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| Nothing merges; entries sit in queue | A required check does not run on `merge_group` | Compare the ruleset's `required_status_checks` against the coverage table above; every context there must have a `merge_group` emitter |
| `CI gate` red on `merge_group` but green on the PR | A real combination failure, or a job skipped that the roll-up does not tolerate | Read the `merge_group` run log; if a legitimately-skipped job is flagged, extend the roll-up tolerance in `ci.yml` |
| `CI gate` green on `merge_group` but the test jobs did not run | The planner classified the group too narrowly | Open the `Determine changes` job log and check the `git diagnostic` group: the merge_group base must be the group's `base_sha`, and the changed-file list must cover every entry in the group |
| Entries ejected as timed out | `check_response_timeout_minutes` below the real CI wall time | Raise it; see the measured distribution in Tunables |
| Merges land but no release is tagged | The post-merge `push` CI run did not reach the dispatcher | Confirm a `push` run on `main` exists at the merged SHA, then that `post-ci-dispatcher.yml` ran off it; check `pyproject.toml` is still absent from `ci.yml`'s `push.paths-ignore` |
| Queue throughput too low | Build concurrency, not batching | Raise `max_entries_to_build`. Do **not** raise `max_entries_to_merge` - see Tunables |

The `merge_group` path is guarded by regression tests in
`tests/unit/test_required_check_canary_workflow_yaml.py` (required-context
coverage plus the shipped `ci-gate` roll-up executed against a synthetic
`merge_group` payload), `tests/unit/test_post_ci_dispatcher_yaml.py` (the
release chain survives the queue) and
`tests/unit/test_merge_queue_runbook_docs.py` (this document stays
internally consistent). If a future change would wedge the queue or break
the release path, those tests fail in CI before it can merge.
