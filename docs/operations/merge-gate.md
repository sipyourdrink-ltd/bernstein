# Merge-gate stack

This document describes the four merge-gate layers that protect `main` from
landing PRs that would put the trunk in a known-bad state, and the operator
steps required to enable each layer.

## TL;DR

| Layer | Workflow | Trigger | What it does | Failure mode |
|---|---|---|---|---|
| 1. Pre-merge autosync | `.github/workflows/pr-policy.yml` (autosync steps) | `pull_request` (acts on `opened, synchronize, ready_for_review`) | Runs `bernstein agents-md sync` and `ruff format`, pushes any drift back to the PR head | Mirror docs go stale and `Repo hygiene` / `docs-drift` fail post-merge |
| 2. Main-red guard | `.github/workflows/pr-policy.yml` (main-red-guard step) | `pull_request` (PRs targeting `main`) | Warns when the most recent completed CI run on main is red and main HEAD still pins the failing SHA | A red main keeps absorbing fresh PR merges instead of being repaired |
| 3. Merge queue + `merge_group:` CI | GitHub-native + `.github/workflows/ci.yml` | `merge_group:` | Re-runs the full CI suite on the combined branch GitHub computes for the queued merge group | Cancelled-by-newer-push races: "green CI" never matches the SHA that actually merges |
| 4. Nightly drift sweep | `.github/workflows/nightly-drift-sweep.yml` | `schedule: 13 6 * * *` + `workflow_dispatch:` | Opens a sync PR when overnight drift accumulated on main | Drift from fork PRs / `skip-autosync` PRs / autosync failures piles up between PR pushes |

## Why we need all four

A single rapid burst of auto-merges flipped `main` red because:

1. `AGENTS.md cross-CLI sync` (the `Repo hygiene` job's drift check), `docs-drift`, and the `ruff format` gate were not all in the required-check list. A PR could auto-merge while one of those mirrors was already stale.
2. There was no merge queue. Six PRs in flight simultaneously each cancelled the others' CI runs via concurrency policy, so "green CI" was only ever recorded against a SHA that was never actually merged.
3. There was no `main-red` guard. After the first failed merge flipped `main` red, the auto-merge flow continued to land additional PRs on top of the red SHA.
4. No central job ran `bernstein agents-md sync` after merge, so drift accumulated across PRs.

Each layer fixes one of those four holes.

## Required setup (post-merge, operator steps)

### 1. Provision `BERNSTEIN_AUTOSYNC_TOKEN` (optional but recommended)

The auto-amend push (the autosync steps in `pr-policy.yml`) and the nightly sweep in `nightly-drift-sweep.yml` both prefer a named token over `GITHUB_TOKEN`. Without the named token everything still works, but amend commits authored by `GITHUB_TOKEN` will NOT trigger downstream workflow runs on the source PR (GitHub recursion protection). That means the PR's CI checks will appear stuck on the previous SHA until something else pushes to the branch.

Choose one of the following:

**Option A: fine-grained PAT (simplest).**

1. Generate a fine-grained PAT scoped to this repo with:
   - `contents: write`
   - `pull-requests: write`
   - `metadata: read` (default)
2. Store under repo secrets as `BERNSTEIN_AUTOSYNC_TOKEN`.
3. Set a 90-day expiry and a reminder to rotate.

**Option B: GitHub App (production-grade).**

1. Create a private GitHub App on the org with permissions:
   - `contents: write`
   - `pull-requests: write`
2. Install the App on this repo.
3. Store the App's installation token under repo secrets as `BERNSTEIN_AUTOSYNC_TOKEN`. Provision an installation-token rotator workflow (Action Marketplace has community options) since installation tokens expire hourly.

### 2. Enable the merge queue on `main`

```bash
# 2a. Enable the merge queue on the main branch ruleset.
gh api -X PATCH "repos/sipyourdrink-ltd/bernstein/branches/main/protection" \
  -H "Accept: application/vnd.github+json" \
  -f required_pull_request_reviews.required_approving_review_count=1 \
  -f required_pull_request_reviews.require_code_owner_reviews=true \
  -F allow_merge_queue=true
```

**2b. Configure the merge-queue policy** using the ruleset payload in
[merge-queue.md :: Enable](merge-queue.md), which is the declared source of
truth for these parameters: the shipped ruleset is reconciled *to* that
document, and `tests/unit/test_merge_queue_runbook_docs.py` holds it to its own
Tunables table. This page deliberately does not restate the values.

Two of them are load-bearing rather than tuning, and are the reason a second
copy is not kept here:

- `max_entries_to_merge` must stay `1`. The auto-release gate keys on the push
  head SHA, so merging N entries in one push skips a version bump anywhere but
  last - green CI, no tag, no publish, and no error anywhere to notice.
- `min_entries_to_merge_wait_minutes` is `0`. With one entry merging per push
  there is no batch to fill, so any wait is pure added latency.

After enabling, `gh pr merge --auto` will route the PR into the merge queue instead of merging immediately. CI then runs against the combined branch GitHub computes for the queued merge group, and the `merge_group:` trigger added to `.github/workflows/ci.yml` makes the existing CI suite respond to that event.

### 3. Require the `CI gate` aggregator

Do not enumerate individual CI job names in `required_status_checks.contexts`. The literal job names drift (the type-check context is `Type check report`, and the test matrix reports sharded contexts such as `Test (ubuntu-latest, Python 3.13, shard 1)`), and a context that never reports wedges every merge on `main`. The `ci-gate` job in `ci.yml` (context `CI gate`) already rolls up every upstream job result with conditional allowed-skips, so it is the single *CI* context to require - the same recommendation as [merge-queue.md](merge-queue.md).

```bash
gh api -X PATCH "repos/sipyourdrink-ltd/bernstein/branches/main/protection" \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks.strict=true \
  -F required_status_checks.contexts[]='CI gate'
```

Notes:

- `CI gate` covers `Repo hygiene`, `Lint`, `Type check report`, `Workflow lint`, `Lineage Gate`, `Bandit (security)`, `pip-audit (deps)`, the full sharded test matrix, and the rest of the `ci.yml` jobs it declares in `needs:`.
- The main-red-guard step in `pr-policy.yml` is advisory (it warns, never fails) and is not a required context.
- `docs-drift / Run drift check` is path-filtered (it does not report on PRs that touch no docs-relevant paths), so it must not be a blanket required check; its main-branch failure mode is the post-merge gate described in the TL;DR.
- `PR policy` (the consolidated per-PR policy job that carries the autosync steps) is intentionally NOT required. The autosync steps run to amend the PR; if the amend fails (e.g. branch-protection rejects the push) we want the next push to retry rather than blocking the merge.

### 4. Verify the layers work end to end

1. Open a PR that intentionally diverges `AGENTS.md` from canonical IR. Confirm the `PR policy` job runs the autosync steps and amends the PR head with a regen commit.
2. Cause `ci.yml` on main to fail (e.g. force-push a known-bad commit to a sandbox branch, then merge with admin override). Open a fresh PR. Confirm the main-red-guard step in `PR policy` emits a warning and job summary pointing at the failing SHA.
3. Trigger the nightly sweep manually via `gh workflow run nightly-drift-sweep.yml`. Confirm it either no-ops (no drift) or opens a sweep PR labelled `automated`.
4. Queue two PRs via auto-merge. Confirm each one gets its own merge-group CI run via the `merge_group:` trigger, and that `main` advances to the exact SHA the queue tested. The queue is configured with `max_entries_to_merge: 1` so one PR lands per push; see [merge-queue.md](merge-queue.md) for why.

## Windows-lane promotion (self-promoting gate)

The `Test (windows-latest, ...)` matrix cells no longer carry a blanket
`continue-on-error: true` mask. The Windows test result now flows through
`scripts/windows_lane_gate.py`, a deterministic gate whose decision is a
pure projection of two inputs:

* the exit code of the Windows test invocation, and
* whether `.github/windows-lane-baseline.json` records an established green
  history (`established: true`).

| result | `established` | gate decision |
|---|---|---|
| green (`0`) | either | pass (exit 0) |
| red (non-zero) | `false` | advisory - `::warning::`, exit 0 (does not block) |
| red (non-zero) | `true` | blocked - `::error::`, exit 1 (fails the check) |

While the baseline is `false` the lane behaves exactly like the old mask
(advisory, never blocks) but surfaces a warning instead of swallowing the
failure silently. Once the readiness gate below holds on a real Windows
runner, promotion is a **one-line data change** - flip `established` to
`true` in the baseline file - not a workflow edit. Rollback is the same
one-line flip back to `false`, so a flaky runner never wedges the merge
queue for the whole team.

Acceptance criterion 3 of the Windows-parity work asks for this lane to
become a required check. The gate mechanism is in place; the flip must
still FOLLOW a green streak on a real Windows runner, not precede it.

### Readiness gate (all must hold before flipping the baseline to `true`)

| Gate | How to check | Why it matters |
|---|---|---|
| Platform-reason skips are individually justified | `git grep -nE "skipif.*(win32|IS_WINDOWS|os.name)" tests/` - the count is reduced from 8 to 5, and every remaining marker needs a real POSIX kernel or the runner cannot run it deterministically: real SIGKILL delivery (`test_adapter_timeout.py`), POSIX session detach + SIGKILL (`test_run_service_detached.py`), the POSIX `resource.setrlimit` module (`test_resource_limits.py`), POSIX process-group signals (`test_worker.py`), and the stub-adapter worker reap which is non-deterministic on the windows-latest runner (`test_canary_real_run.py::test_subprocess_spawn_flow_runs_real_process`) | A required lane that still skips silently is a gate with holes. The file-mode / permission tests and the canary git-worktree flow now run cross-platform (assertions keyed off the platform); only genuinely POSIX-kernel tests plus the one runner-nondeterministic mock-worker reap remain skipped |
| Windows lane green for N consecutive main pushes | Inspect the last N `Test (windows-latest, *)` runs on main (recommend N >= 10, mirrored by `streak_required` in the baseline file) | Promotion before a green streak just moves the trunk into a known-red state |
| Gate is emitting, not masking | The Windows step log shows a `windows-lane-gate: {...}` projection line every run; a red result while `established=false` shows `::warning::`, not a swallowed pass | Confirms the gate is wired and the advisory branch is deliberate, not an accidental mask |
| Conformance stop/restart passes on Windows | The `Adapter conformance + e2e (windows)` job runs `test_adapter_conformance_lifecycle.py` on `windows-latest`, exercising the **real** spawn / stop (platform process-tree reap) / restart contract for claude, codex, and gemini against the cross-platform fake-CLI harness -- not a mock projection. The same job runs the Windows worktree-isolation contract | AC 2 (real stop/restart parity) and the worktree-isolation half of AC 4 are the substance of the lane. The full mock-agent example-goal run remains advisory: the stub-adapter worker reap is non-deterministic on the hosted windows-latest runner, so that one e2e is not gated |

### Flip procedure (operator, after the gate holds)

1. Edit `.github/windows-lane-baseline.json` and set `"established": true`.
   No workflow edit is required - the gate reads this file at run time.

2. Confirm the Windows test result reaches branch protection. The Windows
   matrix cells are `needs:` inputs of the `ci-gate` roll-up, so with the
   `CI gate` context required (section 3 above) a blocked Windows lane
   fails the gate; no literal `Test (windows-latest, ...)` context needs
   to be (or should be) in the required-check list, since shard fan-out
   changes would silently rename it.

3. Land the baseline flip on main via the normal PR flow and watch one full
   Windows lane run go green as a *blocking* check.

### Rollback

If the promoted lane starts flapping, set `"established": false` in
`.github/windows-lane-baseline.json` (single-line revert) and open an issue
with the failing run URL. The lane immediately returns to advisory.

## Per-PR escape hatches

| Condition | Mechanism |
|---|---|
| Disable autosync on a single PR | Add the `skip-autosync` label to the PR |
| Skip merge queue (admin only) | `gh pr merge --admin` bypasses the queue and required checks |
| Skip main-red guard | Not supported. Repair main first |

## Files in this stack

- `.github/workflows/pr-policy.yml` (consolidated per-PR policy job: text hygiene, main-red-guard, trunk andon gate, pre-merge autosync)
- `.github/workflows/nightly-drift-sweep.yml`
- `.github/workflows/ci.yml` (`merge_group:` trigger added under `on:`; Windows test step gated by `scripts/windows_lane_gate.py`)
- `scripts/windows_lane_gate.py` (self-promoting Windows-lane gate)
- `.github/windows-lane-baseline.json` (Windows-lane green-history marker)
- This document
