# Unified observability doctor

`bernstein doctor observe` aggregates the observability backends that
Bernstein integrates with into a single operator-facing table: GitHub
Code Scanning.

## TL;DR

| Command | What it does |
| --- | --- |
| `bernstein doctor observe` | Run all backends, render one Rich table |
| `bernstein doctor observe --json` | Same data as JSON for jq / CI consumption |
| `bernstein doctor observe --watch` | Refresh every 60s until Ctrl-C |
| `bernstein doctor code-scanning` | GitHub Code Scanning-only deep dive |

Backends that are not configured soft-fail to `SKIPPED`, so a fresh
checkout still produces a clean table.

## Backend setup

Each backend reads its credentials from the environment. Set whichever
you have; missing ones soft-fail without error.

| Backend | Required env-vars | Optional env-vars |
| --- | --- | --- |
| `code-scanning` | `GITHUB_TOKEN`, `GITHUB_REPOSITORY` | `GITHUB_API_URL` |

The `GITHUB_TOKEN` used for `code-scanning` must carry
`security_events: read`. The `GITHUB_REPOSITORY` env-var is set
automatically inside GitHub Actions; locally, set it to
`<owner>/<repo>`.

## Output shape

Every probe contributes rows to a single table:

```
backend         metric             value     delta    threshold   status
code-scanning   open_alerts        1         new      0           warn
code-scanning   critical_alerts    0         0        0           ok
code-scanning   high_alerts        2         +1       0           warn
```

The `delta` column is computed against a tiny snapshot cache at
`.sdd/observability/<backend>.json`. Pass `--no-persist` to suppress
the write (handy in CI). Delete the file to reset the baseline.

## JSON contract

`--json` emits one document per invocation. Shape:

```json
{
  "summary": {"ok": 1, "warn": 0, "fail": 0, "skipped": 0, "error": 0},
  "backends": [
    {
      "backend": "code-scanning",
      "status": "ok",
      "detail": "0 open alert(s)",
      "error": null,
      "metrics": [
        {"name": "open_alerts", "value": "0", "numeric": 0.0,
         "threshold": "0", "threshold_status": "ok", "delta": "0"}
      ]
    }
  ]
}
```

Exit code: 0 when every backend is ok or skipped, 1 when any backend
is warn/fail/error.

## CI integration

Two workflows ship alongside the command:

- `.github/workflows/pr-observability-summary.yml`: posts a sticky
  comment on every pull request with the observe table. Triggered on
  `pull_request: [opened, synchronize, reopened]` and via
  `workflow_dispatch` for backfills.
- `.github/workflows/docs-observability-snapshot.yml`: writes today's
  snapshot to
  `docs/_internal/observability/snapshots/<YYYY-MM-DD>.json` and
  re-renders `docs/observability/trends.md` with the last 30 days as
  unicode sparklines. After the render it runs
  `scripts/observability/gate.py`, which diffs today's snapshot against
  yesterday's and reports regressions by reading each row's
  `threshold_status` and computing the numeric delta from the two
  files. It flags a status flip for the worse (`ok -> warn`,
  `* -> fail`), a new or increased security finding, and a backend that
  lost its credentials. Regressions are recorded in the run summary; the
  snapshot pull request still opens regardless.

## Local watch mode

`bernstein doctor observe --watch` re-runs every 60s and refreshes the
Rich table in place. Useful while triaging an incident:

```sh
GITHUB_TOKEN=$(pass github/token) \
GITHUB_REPOSITORY=<owner>/<repo> \
bernstein doctor observe --watch --interval 30
```

Ctrl-C stops the loop and exits 0.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `status: skipped` on a configured backend | env-var typo, wrong prefix | check the table above; `code-scanning` uses `GITHUB_TOKEN`, not `BERNSTEIN_GITHUB_TOKEN` |
| `status: error` with HTTP 401 | token expired or missing scope | regenerate; for code-scanning ensure `security_events: read` |
| `delta: new` on every row | first run, or `.sdd/observability/` deleted | expected; the next run computes signed deltas |
| Sticky PR comment not posted | `pull-requests: write` permission missing | the workflow already requests it; verify the repository allows write actions in PRs |
| Trends document is empty | no daily snapshots have been captured yet | trigger the snapshot workflow manually |
