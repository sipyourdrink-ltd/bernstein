# Merge-gate stack

This document describes the four merge-gate layers that protect `main` from
landing PRs that would put the trunk in a known-bad state, and the operator
steps required to enable each layer.

## TL;DR

| Layer | Workflow | Trigger | What it does | Failure mode |
|---|---|---|---|---|
| 1. Pre-merge autosync | `.github/workflows/pre-merge-autosync.yml` | `pull_request: [opened, synchronize, ready_for_review]` | Runs `bernstein agents-md sync` and `ruff format`, pushes any drift back to the PR head | Mirror docs go stale and `Repo hygiene` / `docs-drift` fail post-merge |
| 2. Main-red guard | `.github/workflows/main-red-guard.yml` | `pull_request: [opened, synchronize, ready_for_review, auto_merge_enabled]` | Fails when the most recent completed CI run on main is red and main HEAD still pins the failing SHA | A red main keeps absorbing fresh PR merges instead of being repaired |
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

The auto-amend push in `pre-merge-autosync.yml` and the nightly sweep in `nightly-drift-sweep.yml` both prefer a named token over `GITHUB_TOKEN`. Without the named token everything still works, but amend commits authored by `GITHUB_TOKEN` will NOT trigger downstream workflow runs on the source PR (GitHub recursion protection). That means the PR's CI checks will appear stuck on the previous SHA until something else pushes to the branch.

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

# 2b. Configure the merge-queue policy (sets max group size and timeouts).
gh api -X PUT "repos/sipyourdrink-ltd/bernstein/branches/main/merge_queue" \
  -H "Accept: application/vnd.github+json" \
  -f merge_method=squash \
  -F max_entries_to_build=5 \
  -F max_entries_to_merge=5 \
  -F min_entries_to_merge=1 \
  -F merge_queue_grouping_strategy=ALLGREEN \
  -F min_entries_to_merge_wait_minutes=5 \
  -F max_entries_to_merge_wait_minutes=60
```

After enabling, `gh pr merge --auto` will route the PR into the merge queue instead of merging immediately. CI then runs against the combined branch GitHub computes for the queued merge group, and the `merge_group:` trigger added to `.github/workflows/ci.yml` makes the existing CI suite respond to that event.

### 3. Expand the required-check list

Set every layer of the gate stack plus the existing CI jobs as required checks. The set below covers the four holes documented in the TL;DR.

```bash
gh api -X PATCH "repos/sipyourdrink-ltd/bernstein/branches/main/protection" \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks.strict=true \
  -F required_status_checks.contexts[]='CI gate' \
  -F required_status_checks.contexts[]='review-bot-ack' \
  -F required_status_checks.contexts[]='Repo hygiene' \
  -F required_status_checks.contexts[]='docs-drift / Run drift check' \
  -F required_status_checks.contexts[]='Lint' \
  -F required_status_checks.contexts[]='Type check' \
  -F required_status_checks.contexts[]='Workflow lint' \
  -F required_status_checks.contexts[]='Lineage Gate' \
  -F required_status_checks.contexts[]='Test (ubuntu-latest, Python 3.12)' \
  -F required_status_checks.contexts[]='Test (ubuntu-latest, Python 3.13)' \
  -F required_status_checks.contexts[]='Test (windows-latest, Python 3.13)' \
  -F required_status_checks.contexts[]='Test (macos-latest, Python 3.13)' \
  -F required_status_checks.contexts[]='Bandit (security)' \
  -F required_status_checks.contexts[]='pip-audit (deps)' \
  -F required_status_checks.contexts[]='main-red-guard'
```

Note that `pre-merge-autosync` is intentionally NOT in the required-check list. The job runs to amend the PR; if the amend fails (e.g. branch-protection rejects the push) we want the next push to retry rather than blocking the merge.

### 4. Verify the layers work end to end

1. Open a PR that intentionally diverges `AGENTS.md` from canonical IR. Confirm `pre-merge-autosync` runs and amends the PR head with a regen commit.
2. Cause `ci.yml` on main to fail (e.g. force-push a known-bad commit to a sandbox branch, then merge with admin override). Open a fresh PR. Confirm `main-red-guard` fails the PR with a clear error pointing at the failing SHA.
3. Trigger the nightly sweep manually via `gh workflow run nightly-drift-sweep.yml`. Confirm it either no-ops (no drift) or opens a sweep PR labelled `automated`.
4. Queue two PRs via auto-merge. Confirm GitHub batches them into a single merge-group CI run via the new `merge_group:` trigger.

## Windows-lane promotion (prepared, not yet flipped)

The `Test (windows-latest, ...)` matrix cells run today with
`continue-on-error: true` on the Windows-only test step in `ci.yml`, so the
lane is advisory: it reports but never blocks a merge. Acceptance criterion
3 of the Windows-parity work asks for this lane to become a required check.
Promotion is a deliberate, reversible operator action that must FOLLOW a
green streak, not precede it. This section documents the readiness gate and
the exact steps so the flip is a checklist, not a judgement call.

### Readiness gate (all must hold before flipping)

| Gate | How to check | Why it matters |
|---|---|---|
| Platform-reason skips are individually justified | `git grep -nE "skipif.*(win32|IS_WINDOWS|os.name)" tests/` - every remaining marker carries an in-code reason that needs a real POSIX kernel (signal delivery, `chmod 0o000`, `setrlimit`, `shlex`) | A required lane that still skips silently is a gate with holes |
| Windows lane green for N consecutive main pushes | Inspect the last N `Test (windows-latest, *)` runs on main (recommend N >= 10) | Promotion before a green streak just moves the trunk into a known-red state |
| No `continue-on-error` masking a real failure | Read the Windows step logs for `##[error]` lines even while the step is green | `continue-on-error` turns a red step green; confirm green means green |
| Conformance stop/restart passes on Windows | `Test (windows-latest, *)` includes the adapter conformance + reap-receipt suites | AC 2 (stop/restart parity) is the substance of the lane |

### Flip procedure (operator, after the gate holds)

1. Remove the mask on the Windows test step in `.github/workflows/ci.yml`:

   ```yaml
   - name: Run isolated test suite (Windows)
     if: runner.os == 'Windows'
     # DELETE the next line to make the lane blocking:
     # continue-on-error: true
     shell: pwsh
     ...
   ```

   The same edit applies to the Windows conformance / integration steps in
   the same job that still carry `continue-on-error: true`.

2. Confirm the Windows contexts are already in the required-check list
   (they are listed in section 3 above:
   `Test (windows-latest, Python 3.13)`). If the shard fan-out changed the
   context name, update section 3 to match the literal job name GitHub
   reports, or branch protection will wait forever on a context that never
   arrives.

3. Land the `continue-on-error` removal on main via the normal PR flow and
   watch one full Windows lane run go green as a *blocking* check.

### Rollback

If the promoted lane starts flapping, restore `continue-on-error: true` on
the Windows step (single-line revert) and open an issue with the failing
run URL. Rollback is intentionally a one-line change so a flaky Windows
runner never wedges the merge queue for the whole team.

> This PR does NOT flip the lane. It documents the gate and the procedure
> only; the `continue-on-error: true` mask stays in place until the
> readiness gate above holds on a real Windows runner.

## Per-PR escape hatches

| Condition | Mechanism |
|---|---|
| Disable autosync on a single PR | Add the `skip-autosync` label to the PR |
| Skip merge queue (admin only) | `gh pr merge --admin` bypasses the queue and required checks |
| Skip main-red guard | Not supported. Repair main first |

## Files in this stack

- `.github/workflows/pre-merge-autosync.yml`
- `.github/workflows/main-red-guard.yml`
- `.github/workflows/nightly-drift-sweep.yml`
- `.github/workflows/ci.yml` (`merge_group:` trigger added under `on:`)
- This document
