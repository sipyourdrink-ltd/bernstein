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
| Baseline provenance | records `line_rate`, `head_sha`, `run_id`; re-derive offline with `coverage_ratchet.py verify` |
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
  "head_sha": "11eb64d14162fd69e060811efe193f87cd36b9cc",
  "line_rate": 0.8281,
  "run_id": "30886183592",
  "total_coverage_percent": 82.81,
  "updated_at": "2026-08-04T06:14:04+00:00"
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
| `line_rate` | raw Cobertura root `line-rate` (0-1) the percentage was rounded from | LEVEL 2 ratchet, with the mark |
| `head_sha` | commit on `main` the measurement was taken against | LEVEL 2 ratchet, with the mark |
| `run_id` | CI run whose `coverage-report` artifact supplied the measurement | LEVEL 2 ratchet, with the mark |

`total_coverage_percent` is seeded from a real measurement of `main` (the
full per-file isolated unit-suite coverage run, identical to the CI shard),
not a guess, so the ratchet starts honest.

### Provenance: the committed number must be reproducible

`total_coverage_percent` is a *generated* value, so the file records what
generated it. The last three keys are that record:

- `line_rate` is the report's own unrounded figure, so the committed
  percentage can be recomputed - `round(line_rate * 100, 2)` - from the
  committed file alone, with no coverage report and no network.
- `head_sha` and `run_id` name the tree and the run behind the number, so
  a mark can be traced back to a measurement instead of being taken on
  trust.

Worked example - the 82.81 mark above came from CI run `30886183592` on
`main` at `11eb64d1`, whose Cobertura root reads
`line-rate="0.8281" lines-valid="233551" lines-covered="193411"`. Both
routes agree, which is why one stored fraction is enough:

| Route | Arithmetic | Result |
|---|---|---|
| stored `line_rate` | `round(0.8281 × 100, 2)` | **82.81** |
| raw counters | `193411 / 233551 × 100` = 82.8132 | **82.81** |

Check it yourself, offline:

```bash
uv run python scripts/coverage_ratchet.py verify \
    --baseline .coverage-baseline.json --require-provenance
```

**These fields are checked, not decorative.** `check` refuses to run
against a baseline whose `total_coverage_percent` is not what its own
`line_rate` rounds to (exit 2, writes nothing), the ratchet re-verifies a
bumped baseline before opening its PR, and a unit test asserts the
committed file carries provenance and re-derives. A hand-edited or
half-updated baseline therefore fails loudly rather than silently
becoming the new truth.

A baseline written before these fields existed has no `line_rate`. That
is a warning, not an error - the ratchet still runs, and the next click
records provenance.

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

## LEVEL 2 - total-coverage monotonic ratchet (per CI run on main)

Prevents backsliding: total coverage may only hold or rise.

Flow (`.github/workflows/coverage-ratchet.yml`, triggered when a **CI run
on `main` completes** - `workflow_run`, any conclusion):

1. Take the `coverage-report` from the CI run that just finished, and
   check out the commit that run measured
   (`github.event.workflow_run.head_sha`). Because the run has completed,
   the artifact either exists now or never will - there is nothing to wait
   for. See
   [Which run supplies the measurement](#which-run-supplies-the-measurement).
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

### When a fire declines to touch the open ratchet PR

There is only ever one ratchet PR, on the fixed branch
`coverage-ratchet/baseline`, so each fire updates it in place. The `guard`
step decides whether this fire may open or update it, and it refuses for
three unrelated reasons. Each prints a `::notice::` naming which one it
was, and all end the run green — nothing is lost by skipping, because the
ratchet is monotonic and idempotent.

| Notice | Meaning | What to do |
|---|---|---|
| `measured N% is not above the M% already committed on the base branch` | `check` read its baseline out of the **measured commit's** tree, and `main` has ratcheted past that mark since. The bump is real against what it read and a no-op against `main`, so the PR would carry only moved `head_sha`/`run_id`. | Nothing. This is the ordinary steady state when ratchet PRs merge promptly. |
| `measured N% is not above the open ratchet PR's M%` | The measurement beats the mark on `main` but not the higher one the open PR already carries. Pushing it would move the high-water mark **down**. | Nothing. Merge the open PR when you are ready; the next rise ratchets from there. |
| `the open ratchet PR is in the merge queue, which locks ... against pushes` | The PR is queued, so GitHub rejects every push to its branch (`GH006`) for the whole time it is in the queue. | Nothing. Once it merges, the branch is free and the next fire opens a fresh PR at whatever the high-water mark is by then. |

The first of those is why a ratchet PR can no longer open carrying nothing
but provenance (issue #4087). It is checked before the open PR is
considered at all, because a provenance-only diff opens a *fresh* PR just
as readily as it rewrites an existing one.

The decision itself is `scripts/coverage_ratchet.py guard`; the workflow
step only gathers the three inputs (the baseline on the ratchet branch,
the baseline on the branch the PR is opened onto, and the head branches
currently in the merge queue) and hands them over. A failed read of any of
them is loud and fails the step rather than defaulting — an unreadable
queue treated as empty would read as "the branch is pushable" and put the
`GH006` failure straight back, and a base baseline treated as absent would
retire the provenance-only refusal while the job stayed green.

### Which run supplies the measurement

The ratchet may only bump on a measurement of the commit being ratcheted.
Two things have to be true at once, and they pull against each other:

- **The right commit.** Resolving by "freshest recent run that happens to
  have a `coverage-report`" takes whatever report exists, including one
  belonging to a different commit. That records a high-water mark for a
  tree nobody can identify and makes the next honest measurement look like
  a regression.
- **A run that has actually finished.** On a `push` trigger the ratchet
  and the CI run start from the same event, so the artifact does not exist
  yet when the ratchet looks. Pinning the commit *without* moving the
  trigger would leave the ratchet correct and permanently idle - it would
  skip every commit.

So the trigger is CI completion, not the push:

```yaml
on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]
    branches: [main]
```

`types: [completed]` deliberately does **not** filter on conclusion.
`ci.yml`'s `cancel-in-progress` concurrency cancels most `main` runs under
the rapid-merge cadence, so firing only on `success` would idle the
ratchet almost permanently - the reason the original implementation
avoided `workflow_run` altogether. A cancelled run has usually already
uploaded `coverage-report` before it was cut, and the event still hands us
an exact `head_sha` and run id.

From there, two ordered passes:

| Pass | Accepts | Why |
|---|---|---|
| 1 | the triggering run itself | the ordinary case; its id and `head_sha` come straight from the event |
| 2 | any **other completed** run for the same `head_sha` | fallback when the triggering run was cut before the shard uploaded |

Pass 2 widens *which run*, never *which commit*, and ignores runs still in
flight (they have not finished uploading). So the worst case is a
**partial** report for the right commit, which understates coverage and
can therefore cost a bump but never manufacture one.

If no completed run for this commit carries a `coverage-report` (a
docs-only push, say), the workflow logs a notice and skips. Skipping is
the correct outcome - the alternative is measuring something else - and
the next commit's CI completion gets its own chance.

> A `workflow_run` workflow always executes the copy of the file on the
> default branch, so edits to this workflow take effect only once merged
> to `main`.

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
| LEVEL 2 reports a drop from a *partial* CI run | when the resolved CI run was cancelled mid-shard, its `coverage.xml` understates coverage and the ratchet flags a spurious drop. The run is still the right commit (resolution filters on `head_sha`), so this is a partial measurement, not a mismatched one. Advisory (warning only) and self-heals on the next complete run. Do **not** lower the baseline for this - it is a measurement artifact, not a real regression. Promote LEVEL 2 to blocking only once full-run artifacts are reliable. |
| Total dips on a pure deletion | the deletion removes covered *and* uncovered lines; if the percentage genuinely dropped, add a test or accept the lower mark - see **Lowering the baseline by hand** below |
| Need to reset the baseline after a large legitimate change | run `scripts/coverage_ratchet.py init --coverage-xml coverage.xml --baseline .coverage-baseline.json --head-sha "$(git rev-parse HEAD)"` against a fresh measurement and commit the result. This is the preferred reset: it rewrites the mark *and* its provenance together. |

### Lowering the baseline by hand

Still the explicit, auditable escape hatch - a committed file change,
visible in review, with `updated_at` and `git blame` showing when and who.
One extra rule now applies:

> **Move `line_rate` with `total_coverage_percent`.** The two must agree
> (`round(line_rate * 100, 2) == total_coverage_percent`), and `head_sha`
> should name the commit you measured. Editing one and leaving the other
> is exactly the half-applied edit the consistency check exists to catch:
> `check` will exit 2 and the unit test will fail.

Confirm the edit before you push it:

```bash
uv run python scripts/coverage_ratchet.py verify \
    --baseline .coverage-baseline.json --require-provenance
```

If you would rather not hand-maintain the pair, use the `init` reset in
the table above instead - it derives both from a real report.

---

## Local usage

```bash
# Compare a local coverage.xml to the baseline (does not write unless it rose).
uv run python scripts/coverage_ratchet.py check \
    --coverage-xml coverage.xml --baseline .coverage-baseline.json --no-bump

# Re-derive the committed percentage from the baseline alone.
# Needs no coverage.xml and no network - this is the offline check.
uv run python scripts/coverage_ratchet.py verify \
    --baseline .coverage-baseline.json --require-provenance

# Print the current diff-coverage floor.
uv run python scripts/coverage_ratchet.py show-floor --baseline .coverage-baseline.json

# Re-seed the baseline from a fresh measurement.
uv run python scripts/coverage_ratchet.py init \
    --coverage-xml coverage.xml --baseline .coverage-baseline.json --diff-floor 80 \
    --head-sha "$(git rev-parse HEAD)"
```

Exit codes: `0` held/rose, `1` dropped (advisory), `2` misconfiguration
(including a baseline that does not re-derive), `3` missing/malformed
`coverage.xml` (soft-skip).

`--require-provenance` also fails when the baseline records no `line_rate`
or no `head_sha`; without the flag those are a warning, so a baseline
predating provenance still verifies.

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
