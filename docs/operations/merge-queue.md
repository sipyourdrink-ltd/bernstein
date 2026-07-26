# Merge queue runbook

Operator-facing notes on the GitHub merge queue for `main`. The queue is
configured by a repository **ruleset** (not legacy branch protection).

**This document is the source of truth for the queue configuration.** The
shipped ruleset is currently out of sync with it; see
[Tunables](#tunables-source-of-truth) for the reconciliation and
[Enable](#enable-mechanical-steps) for the exact calls that close the gap.

## TL;DR

| Topic | Status | Where |
|-------|--------|-------|
| Queue state today | Ruleset exists, `enforcement: disabled` | ruleset `main-merge-queue` |
| What it solves | Tests the A+B *combination* before merge | this doc |
| Required checks on the queue | `CI gate` **and** `review-bot-ack` | `ci.yml`, `review-bot-ack.yml` |
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
| **Enter the queue** | Legacy branch-protection required checks (`CI gate` + `review-bot-ack`) | `pull_request` |
| **Merge from the queue** | Ruleset `required_status_checks` (must be `CI gate` + `review-bot-ack`) | `merge_group` |

`CI gate` (the `ci-gate` aggregator job in `ci.yml`) runs on `merge_group`
because the workflow declares `merge_group: {}` and `ci-gate` has
`if: always() && !cancelled()` with no event exclusion.

### Required-check coverage under `merge_group`

Every context required on a PR also reports on a `merge_group` ref:

| Required context | Emitting workflow | Runs on `merge_group`? |
|------------------|-------------------|------------------------|
| `CI gate` | `ci.yml` :: `ci-gate` | Yes - `merge_group: {}` |
| `CI gate` | `ci-gate-stub.yml` :: `ci-gate` | No - `pull_request` only, and **correct** (see below) |
| `review-bot-ack` | `review-bot-ack.yml` :: `merge-group-pass` | Yes - `if: github.event_name == 'merge_group'` |

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
`tests/unit/test_required_check_canary_workflow_yaml.py`.

> **`review-bot-ack` belongs in the ruleset `required_status_checks`.**
> An earlier revision of this runbook said the opposite, on the grounds
> that the workflow triggered only on `pull_request` /
> `pull_request_review`. That is no longer true: `review-bot-ack.yml`
> declares `merge_group: {}` and carries a `merge-group-pass` job that
> republishes the identical `review-bot-ack` context on the queue's
> ephemeral ref. Requiring it on the queue cannot wedge anything, and
> leaving it out would leave two divergent definitions of "required" -
> the PR gate enforcing two contexts and the queue enforcing one.

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
Yes. When a merge group goes green, GitHub advances the base branch to the
**same SHA** that the queue tested and emits an ordinary `push` event on
that branch. The push is attributed to `github-merge-queue[bot]` and is
not suppressed by Actions' `GITHUB_TOKEN` loop prevention, so
`push`-triggered workflows start normally.

Observed on a public repository running the queue at scale
(`cilium/cilium`, 2026-07-24):

| Event | `head_branch` | `head_sha` | Actor |
|-------|---------------|-----------|-------|
| `merge_group` run, 23:21:05Z | `gh-readonly-queue/main/pr-47100-67dfc5a2…` | `90c7690d43…` | - |
| `push` run, 23:34:05Z | `main` | `90c7690d43…` (identical) | `github-merge-queue[bot]` |

So the chain is unchanged end to end:

```
queue merges -> push to main (same SHA) -> ci.yml (push) -> workflow_run
             -> post-ci-dispatcher (branches: [main]) -> auto-release -> tag -> publish
```

`pyproject.toml` is deliberately **not** in `ci.yml`'s `push.paths-ignore`
list, so a version-bump commit always triggers the CI run the listener
needs. That is asserted by `tests/unit/test_post_ci_dispatcher_yaml.py`.

**The invariant this depends on: one PR per push.** The release gate keys
on the push's head SHA. If a merge group merged N > 1 entries at once, the
base branch would advance by N commits in a single push, and the push
would report only the last entry's SHA. A version bump sitting anywhere
but last in the batch would be skipped silently - green CI, no tag, no
publish, no error. `max_entries_to_merge` is pinned to `1` for this
reason; see [Tunables](#tunables-source-of-truth).

## Tunables (source of truth)

The values below are authoritative. **The shipped ruleset does not match
them yet** - the "Shipped" column records the drift to be corrected when
the queue is enabled.

| Parameter | Shipped | Correct | Why |
|-----------|---------|---------|-----|
| `merge_method` | `SQUASH` | `SQUASH` | One commit per PR on `main` |
| `grouping_strategy` | `ALLGREEN` | `ALLGREEN` | A group merges only if every entry is green |
| `max_entries_to_build` | 1 | **5** | Build concurrency: the number of `merge_group` webhooks in flight. At `1` the queue tests one entry at a time, so a burst of N PRs costs N sequential full CI cycles - strictly worse latency than today with no load benefit the per-SHA policy does not already provide. `5` caps concurrent queue CI at 5 instead of the unbounded N a burst produces today. |
| `min_entries_to_merge` | 1 | **1** | Never wait for a second entry before merging |
| `max_entries_to_merge` | 1 | **1** | One PR lands per push to `main`. The auto-release gate keys on the push head SHA, so merging N entries in a single push silently skips a version bump that is not last in the batch - green CI, no tag, no publish, no error. Lifting this requires the release gate to stop keying on the push head SHA; tracked as a follow-up. The second former blocker is closed: `determine-changes` now classifies a queued group against `github.event.merge_group.base_sha`, so a multi-entry group is planned from its combined diff instead of its last commit. |
| `min_entries_to_merge_wait_minutes` | 0 | **0** | With `max_entries_to_merge = 1` there is no batch to fill, so any wait is pure added latency |
| `check_response_timeout_minutes` | 30 | **120** | Measured over the last 30 successful `CI` runs on `main`: p50 38 min, p90 58 min, max 105 min. At `30` **every** entry would be ejected as timed out; at `60` roughly one in ten would be. `120` clears the observed maximum and the `test` job's own 90-minute budget. |

## Enable (mechanical steps)

**Do not run these while a release is in flight.** Enabling the queue
serialises every open PR through it. The flip is deliberately deferred
until after `v3.10.0` ships.

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
exactly two contexts - `CI gate` and `review-bot-ack`, both `app_id`
`15368` (GitHub Actions).

**Step 1 - correct the rules while still disabled.** This applies the
tunables above and adds `review-bot-ack` to the queue's required checks.
Keep `enforcement: disabled` in this payload: a mis-typed rule set then
cannot enable the queue as a side effect.

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
        "check_response_timeout_minutes": 120
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": true,
        "required_status_checks": [
          { "context": "CI gate", "integration_id": 15368 },
          { "context": "review-bot-ack", "integration_id": 15368 }
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
2. Both `CI gate` and `review-bot-ack` report on it.
3. The PR merges, and `main` advances to the **same SHA** the queue tested.
4. A `push` run on `main` appears at that SHA, actor `github-merge-queue[bot]`.
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
