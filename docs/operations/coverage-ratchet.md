# Coverage ratchet

**Audience:** operators maintaining the Bernstein test-coverage gate.

**What:** a two-level, one-way coverage gate. Coverage can only hold or
rise; a drop is reported but (initially) does not block. The floor for
new code nudges up over time so the dark, untested share of the codebase
shrinks instead of growing.

**Why:** only a fraction of the code is exercised by the suite today, so
runtime bugs hide in the untested remainder. The ratchet fixes the root
cause - new code arrives covered - and prevents backsliding without a new
scanner.

---

## TL;DR

| Item | Value |
|---|---|
| LEVEL 1 - diff floor | every PR's *changed* lines must hit a minimum diff coverage |
| LEVEL 2 - total ratchet | total coverage may never drop below the committed high-water mark |
| Baseline file | `.coverage-baseline.json` (repo root) |
| Ratchet script | `scripts/coverage_ratchet.py` |
| Posture | both ADVISORY (report red, never block) until promoted |
| Weekly bump | `coverage-ratchet-weekly.yml` raises the diff floor; opens a review PR |
| Promotion | remove `continue-on-error` (LEVEL 1) / add to required checks (LEVEL 2) |

Both levels reuse machinery already in the repo (`diff-cover` and the CI
coverage shard's `coverage.xml`). There is no parallel coverage system.

---

## The baseline file

`.coverage-baseline.json` is the single source of truth. It is committed
to the repo and updated by the ratchet, never hand-edited in normal flow.

```json
{
  "diff_coverage_floor_percent": 85,
  "total_coverage_percent": 77.51,
  "updated_at": "2026-05-22T00:00:00+00:00"
}
```

The starting diff floor is **85%** - one step above the 80% the diff-cover
step enforced before the ratchet, so new code clears a slightly higher bar
than the legacy default. The weekly bump continues raising it from there.
This is an operator-tunable choice; lower it in the baseline file if 85% is
too steep for the current trunk.

| Key | Meaning | Moved by |
|---|---|---|
| `total_coverage_percent` | high-water mark of total line coverage on `main` | LEVEL 2 ratchet, on a rise |
| `diff_coverage_floor_percent` | minimum diff coverage every PR must hit | weekly bump PR |
| `updated_at` | ISO-8601 UTC timestamp of the last write | every write |

`total_coverage_percent` is seeded from a real measurement of `main` (the
full per-file isolated unit-suite coverage run, identical to the CI shard),
not a guess, so the ratchet starts honest.

Note: this measured total can differ from the figure a static-analysis
dashboard reports. The dashboard ingests whatever `coverage.xml` the CI
shard last uploaded, and under the rapid-merge cadence that artifact is
frequently partial (the shard is cancelled mid-run by `cancel-in-progress`
concurrency) - which understates coverage. The baseline here is the
complete-run number, which is the value the ratchet must protect.

---

## LEVEL 1 - diff-coverage floor (per PR)

Fixes the *root cause* of dark code: new code must arrive covered.

- The `diff-coverage` job in `.github/workflows/ci.yml` runs
  `diff-cover coverage.xml --fail-under=<floor>` on the lines the PR
  changed, relative to the base branch.
- `<floor>` is read at job time from `diff_coverage_floor_percent` in the
  baseline (step `Resolve diff-coverage floor from baseline`), so the
  weekly bump and the gate share one number.
- The job reuses the `coverage.xml` the main test job uploaded as the
  `coverage-report` artifact. No second coverage run.

**Advisory mechanism.** The `Run diff-cover` step is
`continue-on-error: true`, so the *job* result is always `success` even
when diff coverage is below the floor. That is why the job can stay in
the CI-gate `needs` set without ever wedging the merge queue. The shortfall
is reported as a warning and in the step summary; it does not fail the PR.

---

## LEVEL 2 - total-coverage monotonic ratchet (per push to main)

Prevents backsliding: total coverage may only hold or rise.

Flow (`.github/workflows/coverage-ratchet.yml`, triggered on push to
`main`):

1. Resolve the freshest CI run that actually uploaded a `coverage-report`
   artifact and download it (the same `coverage.xml` the shard produced).
   Under the rapid-merge cadence ci.yml's
   `cancel-in-progress` concurrency cancels most main CI runs, so a
   `workflow_run`/`conclusion == success` trigger almost never fires;
   searching recent runs for the artifact (cancelled runs may still carry
   it) is the robust pattern.
2. `scripts/coverage_ratchet.py check` parses the root `line-rate` and
   compares it to `total_coverage_percent`:
   - **measured < baseline** (beyond a 0.05 pp float-jitter tolerance):
     report a drop, exit non-zero. ADVISORY - the step is
     `continue-on-error` and the workflow is **not** in the required-check
     set, so a drop never blocks a merge.
   - **measured > baseline:** the ratchet *clicks* - rewrite the baseline
     to the new high-water mark and open a PR with that one-line change.
   - **flat:** no change.

The bump is a **PR, not a direct push**: `main` is protected by required
status checks, so a bot commit pushed straight to `main` would be rejected.
Opening a PR is the protection-safe path and matches the repo convention
(weekly floor bump). Every baseline movement is therefore a
reviewable, auditable artifact rather than a silent rewrite. Merge the PR
to record the new high-water mark.

The baseline write lives in this separate workflow (not in `ci.yml`) so
`ci.yml`'s gate jobs never need `contents: write`.

### Why a missing coverage.xml is not a drop

Docs-only pushes skip the coverage shard, so `coverage.xml` may be absent.
The script treats a missing or malformed report as a **soft-skip**
(exit 3, warning) - never as a coverage drop - so the ratchet cannot
false-fail on a push that legitimately produced no coverage.

---

## The weekly bump (nudges up over time)

`.github/workflows/coverage-ratchet-weekly.yml`:

- Runs `scripts/coverage_ratchet.py bump-floor` once a week.
- Raises `diff_coverage_floor_percent` by `step` (default **+1 pp**),
  capped at `cap` (default **90%**). The increment is gentle on purpose so
  the floor creeps up without becoming a wall.
- Opens a PR with only the baseline change for operator review. The floor
  moves only when that PR merges; close it to decline a given week's bump.
- If the floor is already at the cap, the run is a clean no-op (no PR).

**Cron is disabled by default.** `ENABLE_CRON` is `"0"` in the workflow
file. A scheduled fire is a no-op until an operator flips it to `"1"` in a
follow-up PR, after a clean `workflow_dispatch` smoke run. This bounds
first-day blast radius.

To smoke-test: run the workflow via **Actions -> Coverage ratchet (weekly
floor bump) -> Run workflow**. Confirm the review PR looks right, then flip
`ENABLE_CRON` to `"1"`.

---

## Promoting from advisory to required

Do this only once coverage is healthy enough that the gates rarely fire.

### Promote LEVEL 1 (diff floor) to blocking

1. In `.github/workflows/ci.yml`, remove `continue-on-error: true` from the
   `Run diff-cover` step in the `diff-coverage` job.
2. The job now fails when diff coverage is below the floor. It is already
   in the CI-gate `needs` set, so the gate will then enforce it.
3. Watch a few PRs to confirm the floor is realistic before tightening it
   further via the weekly bump.

### Promote LEVEL 2 (total ratchet) to blocking

The total ratchet runs *post-merge* (on push to `main`), so it is
structurally advisory: it cannot block a PR merge. To make a total drop
actionable as a hard signal:

1. Remove `continue-on-error: true` from the `Run total-coverage ratchet`
   step in `coverage-ratchet.yml` so the workflow run goes red on a drop.
2. Optionally wire the red `coverage-ratchet` workflow conclusion into the
   trunk-health / main-red-guard surface so a post-merge coverage drop
   raises the same alarm as a broken build.
3. Do **not** add this workflow to branch protection's required checks - it
   is a post-merge workflow, not a PR check, and adding it there would wedge
   the merge queue.

---

## Override for a legitimate coverage-neutral refactor

Sometimes a PR legitimately moves code without changing behaviour (pure
rename, file split, dead-code deletion) and trips the diff floor or dips
total coverage. Options, least to most invasive:

| Situation | Override |
|---|---|
| LEVEL 1 false-positive on a PR | gate is advisory by default - no action needed; if promoted, add the missing tests or split the refactor from the behaviour change |
| LEVEL 2 reports a drop from a *partial* CI run | when the resolved CI run was cancelled mid-shard, its `coverage.xml` understates coverage and the ratchet flags a spurious drop. This is advisory (warning only) and self-heals on the next complete run. Do **not** lower the baseline for this - it is a measurement artifact, not a real regression. Promote LEVEL 2 to blocking only once full-run artifacts are reliable. |
| Total dips on a pure deletion | the deletion removes covered *and* uncovered lines; if the percentage genuinely dropped, add a test or accept the lower baseline by editing `total_coverage_percent` down in the same PR, with a one-line justification in the PR body |
| Need to reset the baseline after a large legitimate change | run `scripts/coverage_ratchet.py init --coverage-xml coverage.xml --baseline .coverage-baseline.json` against a fresh measurement and commit the result |

Editing `total_coverage_percent` downward is the explicit, auditable escape
hatch: it is a committed file change, visible in review, with the
`updated_at` stamp showing when and (via git blame) who.

---

## Local usage

```bash
# Compare a local coverage.xml to the baseline (does not write unless it rose).
uv run python scripts/coverage_ratchet.py check \
    --coverage-xml coverage.xml --baseline .coverage-baseline.json --no-bump

# Print the current diff-coverage floor.
uv run python scripts/coverage_ratchet.py show-floor --baseline .coverage-baseline.json

# Re-seed the baseline from a fresh measurement.
uv run python scripts/coverage_ratchet.py init \
    --coverage-xml coverage.xml --baseline .coverage-baseline.json --diff-floor 80
```

Exit codes: `0` held/rose, `1` dropped (advisory), `2` misconfiguration,
`3` missing/malformed `coverage.xml` (soft-skip).

---

## Files

| Path | Role |
|---|---|
| `scripts/coverage_ratchet.py` | compare / bump / seed logic |
| `.coverage-baseline.json` | committed baseline (high-water + floor) |
| `.github/workflows/ci.yml` (`diff-coverage` job) | LEVEL 1 per-PR gate |
| `.github/workflows/coverage-ratchet.yml` | LEVEL 2 post-push total ratchet |
| `.github/workflows/coverage-ratchet-weekly.yml` | weekly floor bump PR |
| `tests/unit/test_coverage_ratchet.py` | unit tests for the script |
