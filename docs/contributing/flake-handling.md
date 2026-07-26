# Flake handling

How Bernstein detects, quarantines, and recovers from flaky tests.

## TL;DR

- One piece of CI machinery: the ctrf-io test reporter (per-PR
  markdown summary). Quarantine itself is a manual operator action.
- There is no automated flake detector. The scheduled `pytest-xflaky`
  hunter was removed after 67 fires produced 0 completed runs and 0
  quarantine decisions; see "Detecting a flake" for the manual
  procedure that replaces it.
- A "flaky" test is one that fails at least twice AND passes at least
  twice across five consecutive runs of the unit suite under
  randomised ordering.
- Quarantined tests get `@pytest.mark.xfail(strict=False)` - they
  still run, still emit XPASS when they pass, but stop blocking the
  merge gate.
- Unquarantining requires three consecutive green runs after the
  decorator is removed.

## Pipeline

### 1. Per-PR test summary (ctrf)

`ctrf-io/github-test-reporter` runs as a step in `ci.yml::test` after
the JUnit producer step. It consumes the same `junit.xml` that
mikepenz/action-junit-report already publishes and:

- Writes a markdown summary to the workflow Step Summary tab.
- Posts (or updates) a sticky comment on the triggering PR with the
  same content.
- Uploads the converted CTRF JSON as the `ctrf-report` workflow
  artifact (7-day retention, sized to the xflaky look-back window).

The reporter highlights failed tests, slowest tests, and previously-
flaky tests when present.

### 2. Flake detection (manual, pytest-xflaky)

There is no scheduled flake hunter. A workflow previously ran this on
a nightly cron, but it never completed a single run in 67 fires and
so never made a quarantine decision. It was removed rather than left
in place, because a detector that appears to exist and does nothing
suppresses the question of whether flakes are being caught at all.

Run the same detection locally when a test looks unstable. Do not run
the whole suite in one invocation; it is memory-hungry. Point it at
the directory or file under suspicion:

1. Install the `dev` group plus `pytest-randomly`. Keep the latter out
   of the global dev deps - it auto-activates on import and would
   shuffle every other CI job's test order.
2. Run `pytest <path> --xflaky-collect --json-report` five times: once
   with `-p no:randomly` for a deterministic baseline, then four times
   with `--randomly-seed=last` to chain seeds.
3. Generate the reports with `--xflaky-report --xflaky-github-report`
   and the threshold `--xflaky-min-failures 2 --xflaky-min-successes 2`.
4. If any test is flagged, run `pytest --xflaky-fix` to rewrite the
   offending test files with `@pytest.mark.xfail(strict=False)`.
5. Open a quarantine PR against `main` with the label set
   `ci, tests, flaky, bot`, and record the threshold and the seeds you
   used in the PR body.

Restoring an automated hunter is reasonable, but it needs an owner who
will confirm it runs to completion; that is the failure the removed
workflow never surfaced.

## Operator workflow

### Reviewing a quarantine PR

1. Open the quarantine PR.
2. Read the affected test names. If they cluster around a single
   subsystem (network, async event loops, filesystem races), that is
   strong evidence of a shared root cause - file a bug to track the
   underlying defect.
3. Read the seeds and per-run detail recorded in the PR body.
4. Land the PR if the markers look reasonable. Close it if the run
   was infra noise - a real flake will resurface.

### Investigating a quarantined test

1. Pull the branch locally.
2. Reproduce in isolation:
   ```
   uv run pytest path/to/test_file.py::TestClass::test_method -x -v
   ```
3. Reproduce under randomised order:
   ```
   uv run pytest path/to/test_file.py --randomly-seed=<seed-from-report>
   ```
4. Reproduce under parallel execution (if parallel-safe):
   ```
   uv run pytest path/to/test_file.py -n auto
   ```
5. Common root causes for our codebase:
   - Shared mutable global state across the agent registry.
   - Hidden network calls (use `respx` to make them deterministic).
   - Time-of-day assumptions (use `freezegun`).
   - Filesystem races on `tmp_path` cleanup.
   - Event-loop bleed between async tests (check pytest-asyncio mode).
6. Fix the root cause, remove the decorator, push.

### Unquarantining policy

A test may be removed from quarantine when:

- The root cause is identified and fixed (preferred).
- The test passes three consecutive runs after the decorator is
  removed (operator runs `gh workflow run ci.yml --ref <branch>`
  two extra times after the initial run).

If neither condition holds, the quarantine stays and the underlying
defect is tracked as a regular bug.

## Threshold tuning

The current thresholds (5 runs, min 2 failures, min 2 successes)
trade off detection latency for false-positive rate:

- Lower `--xflaky-min-failures` would catch slower-flaking tests
  faster but produce false-positive quarantines more often.
- More runs would tighten the signal at the cost of wall-clock time -
  the five-run budget is roughly 25 minutes for the unit suite.

Both knobs are flags on the commands above. Pass them per invocation.

## Out of scope

- Auto-unquarantine after a green-run streak. Easy to get wrong
  (XPASS noise vs. real recovery); tracked as a separate ticket.
- BuildPulse / flaky.io paid SaaS. Free-tier-only constraint.
- Test isolation primitives (`pytest-randomly` as a global dev dep).
  pytest-randomly stays local to the nightly job for the reason
  above.
