# SonarQube integration

Bernstein surfaces SonarQube measures (coverage, code smells, bugs,
vulnerabilities, security hotspots, cognitive-complexity hotspots) in
the operator's terminal so the daily diagnostic flow includes static
analysis without a context switch to the Sonar UI.

## TL;DR

- CI publishes coverage to SonarQube via `.github/workflows/sonar-scan.yml`.
- Operators run `bernstein doctor sonar` to pull the current measures.
- A periodic nudge fires from the parent `bernstein doctor` group when
  thresholds (>50 open code smells or any new vulnerability) are crossed.
- PRs receive a sticky advisory comment summarising the project-level
  numbers. The comment never blocks merge.

## Configuration

The doctor reads two env vars (identical to the CI contract):

| Env var | Purpose | Example |
|---|---|---|
| `SONAR_HOST_URL` | Sonar server base URL | `https://sonar.example.com` |
| `SONAR_TOKEN` | User token with `Browse` permission on the project | (45 chars) |
| `SONAR_PROJECT_KEY` | Optional. Defaults to `bernstein`. | `bernstein` |

If either of the first two is missing, the doctor soft-fails with a
one-line hint and exit code 0. No operator workflow is interrupted.

## CLI

```
bernstein doctor sonar             # rich table view
bernstein doctor sonar --json      # machine-readable snapshot
bernstein doctor sonar --smell-threshold 100  # custom nudge threshold
bernstein doctor sonar --no-update-baseline   # do not persist a new baseline
```

Output sections, default mode:

- Headline measures: coverage, code smells (total), bugs, vulnerabilities,
  security hotspots, cognitive complexity, ncloc.
- Smells by severity: BLOCKER / CRITICAL / MAJOR / MINOR / INFO counts.
- Cognitive complexity hotspots: top-5 files by `cognitive_complexity`.
- Nudge panel: only shown when the snapshot crosses a threshold.

JSON mode emits a single object with the same fields plus an
embedded `nudge` block (`should_nudge`, `reasons`, `smell_threshold`).

## Baseline file

The last-seen counts are cached at:

```
$XDG_DATA_HOME/bernstein/sonar-baseline.json
```

(Defaulting to `~/.local/share/bernstein/sonar-baseline.json` when
`XDG_DATA_HOME` is unset.)

The file is written on every successful `bernstein doctor sonar` run
unless `--no-update-baseline` is passed. Deleting the file resets the
nudge so the next run records a fresh baseline instead of firing.

## Periodic nudge

When the operator runs the parent `bernstein doctor` (no subcommand)
or `bernstein doctor --suggest-docs`, the group appends a single-line
yellow hint when either of the following is true:

- The current snapshot reports more than 50 open code smells.
- The current snapshot reports more vulnerabilities than the baseline.

The hint points back at `bernstein doctor sonar` for the full surface.
The nudge is suppressed under `--json` so machine-readable output stays
clean.

## CI workflows

One workflow drives the integration:

- `.github/workflows/sonar-scan.yml` runs after the main CI workflow
  succeeds on `main`, downloads the coverage artifact, and pushes a
  scan to the Sonar server. Triggered via `workflow_run`.

Findings surface through the code-scanning (SARIF) path; there is no
per-PR comment workflow. Run `bernstein doctor sonar` locally for the
current project measures.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Sonar integration not configured` | Env vars unset | Export `SONAR_HOST_URL` / `SONAR_TOKEN`. |
| `server unreachable or project not yet scanned` | Project key not yet present on the server | Wait for the next `sonar-scan` workflow run on `main`. |
| `403` from `/api/measures/component` | Token lacks `Browse` permission | Regenerate the token with the right scope. |

## Implementation map

| Module | Purpose |
|---|---|
| `src/bernstein/core/observability/sonar.py` | API client, baseline I/O, nudge logic. |
| `src/bernstein/cli/commands/doctor_sonar_cmd.py` | Click command + Rich renderer. |
| `tests/unit/cli/doctor/test_sonar.py` | Coverage for the client, baseline, nudge, and CLI wiring. |
| `.github/workflows/sonar-scan.yml` | Scan workflow (push to main). |
