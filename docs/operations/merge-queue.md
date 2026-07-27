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
| Safe to flip today? | **No** - 4 blockers, one substantive | [Blockers](#blockers-to-the-flip) |
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
| `review-bot-ack` | `review-bot-ack.yml` :: `merge-group-verify` | Yes - `if: github.event_name == 'merge_group'` |

The queue-side emitter checks out
`github.event.merge_group.base_ref`, not the group. `actions/checkout`
with no `ref:` takes the triggering ref, which on a `merge_group` event is
the ephemeral `gh-readonly-queue/...` branch carrying the *candidate pull
request's tree*. That job holds `checks: write`, and `checks: write` is
enough to post a completed check-run named `CI gate` with conclusion
`success` on any commit in the repository - so running its publisher out
of a candidate tree would hand the entry's author the ability to forge the
other required context. Pinning the base ref is the same reasoning that
pins the `pr-gate` job to the pull request's base ref. Locked by
`tests/unit/test_merge_queue_gate_coverage_yaml.py` and
`tests/unit/test_review_bot_ack_workflow_yaml.py`.

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
> declares `merge_group: {}` and carries a `merge-group-verify` job that
> publishes the identical `review-bot-ack` context on the queue's
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
| `check_response_timeout_minutes` | 30 | **240** | Re-measured 2026-07-27 over the last 30 successful `CI` runs on `main` (`run_started_at` -> `updated_at`): p50 **49** min, p90 **214** min, max **243** min, and **30 of 30 exceeded 30 minutes**. The earlier figures in this row (p50 38 / p90 58 / max 105) are stale. At the shipped `30`, *every* entry is ejected as timed out. At `120`, 9 of those 30 runs would still have been ejected. `240` covers all but the single 243-minute outlier. Re-measure in Step 0 before flipping - this value tracks runner-pool contention, not the test suite. |

## Blockers to the flip

Measured 2026-07-27. These are conditions on the repository, not code
changes; none of them is fixed by a pull request. Until each is cleared,
enabling the queue makes `main` slower to advance without making it
safer.

| # | Blocker | Evidence | Clears when |
|---|---------|----------|-------------|
| 1 | The shipped ruleset would eject **every** entry. `check_response_timeout_minutes` is `30`; the last 30 successful `CI` runs on `main` all took longer than that. | p50 49 min, p90 214 min, max 243 min, 30/30 over 30 min | Step 1 is applied (raises it to `240`) |
| 2 | The shipped ruleset requires only `CI gate`. `review-bot-ack` is absent, so the queue would merge without the gate that branch protection requires at entry. | `gh api repos/$REPO/rulesets/16719298 --jq '.rules'` -> one context | Step 1 is applied |
| 3 | CI wall time makes serialised merging impractical. `max_entries_to_build` is the number of groups built at once, but `max_entries_to_merge` is pinned to `1` for the release path, so a burst of N ready PRs costs N sequential merges. At p90 = 214 min that is most of a day for five PRs. | same distribution as #1 | CI wall time is bounded, or the release gate stops keying on the push head SHA so batches can merge |
| 4 | The queue-side `review-bot-ack` emitter has never executed. `merge-group-verify` was written after the last live queue window (2026-05-22) and the queue has been disabled since. A required context whose emitter has never run is exactly the "waits forever on a check that cannot report" failure. | `gh run list --event merge_group` -> newest run 2026-05-22T02:04:46Z | The Step 4 smoke test observes it reporting on one real entry |

Blocker 3 is the substantive one. Blockers 1 and 2 are two `gh api` calls;
blocker 4 can only be retired by flipping once and watching. So the
recommended order is: apply Step 1, fix CI wall time, then flip with a
single low-risk pull request as the canary and Step 4 open in front of
you.

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
        "check_response_timeout_minutes": 240
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
2. Both `CI gate` **and** `review-bot-ack` report on it. `review-bot-ack`
   is the one to watch: its queue-side emitter (`merge-group-verify` in
   `review-bot-ack.yml`) has never executed against a live queue, because
   it was written after the 2026-05-22 window and the queue has been
   disabled since. If it does not report, the entry sits forever - pause
   immediately rather than waiting it out.
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
