# SonarQube findings tracker

A single consolidated GitHub issue, auto-rendered from the live Sonar API,
that mirrors the current open-finding set. An agent (or an operator) works
the thread top-down; the next scan drops items that have been fixed.

This is the consolidated counterpart to the per-finding sweeper documented
in [`sonar.md`](../observability/sonar.md). The sweeper emits one backlog
ticket per finding; the tracker keeps one living issue thread.

## TL;DR

- One issue titled **SonarQube findings tracker**, labelled `sonar-tracker`
  and `automated`.
- Re-rendered on every run; the same issue is edited, never duplicated.
- Found by a hidden marker `<!-- sonar-tracker:bernstein -->` in its body.
- BLOCKER + CRITICAL listed in full as checkboxes with deep links.
- MAJOR / MINOR / INFO collapsed into `<details>` blocks.
- A trailing fenced ```json``` block carries a machine-readable summary so a
  downstream fixer agent can dispatch work without re-parsing the markdown.
- Workflow: `.github/workflows/sonar-tracker.yml`.
- Script: `scripts/render_sonar_tracker.py`.

## How the loop closes

| Step | What happens |
|---|---|
| 1 | A push to `main` runs the **SonarQube scan** workflow. |
| 2 | On scan success, `workflow_run` fires `sonar-tracker.yml`. |
| 3 | The script polls `/api/issues/search` (paginated), the quality gate, and coverage. |
| 4 | It renders one markdown body and creates or edits the tracker issue. |
| 5 | An agent fixes code or marks an item Won't Fix in Sonar. |
| 6 | The next scan + render drops the resolved item from the thread. |

A daily `schedule` cron is a backstop so the thread never goes stale even if
no push lands. A manual `workflow_dispatch` run is always available.

## The body

| Region | Content |
|---|---|
| TL;DR table | quality-gate status, coverage %, count per severity |
| BLOCKER section | every finding as `- [ ] rule \`<rule>\`: <label> \`<file>:<line>\` ([view](...))` |
| CRITICAL section | same shape as BLOCKER |
| MAJOR / MINOR / INFO | collapsed `<details>`; capped at 80 items each with an "and N more, see Sonar" pointer |
| JSON summary | fenced ```json``` block (see below) |

### Public-artefact note

A GitHub issue in this repository is a public artefact. The renderer never
copies the raw Sonar `message`/`htmlDesc` text into the body. Every
human-readable label is synthesised from the shared pre-vetted rule-family
blurb table in `scripts/sweep_sonar_findings.py` (with a neutral default
keyed only on the rule id). The fully rendered body is then scanned against
a forbidden-substring guard before it is written.

### Size cap

GitHub rejects issue bodies above 65536 characters. The renderer measures
the body and, if it is over, collapses progressively (lowest severity first)
to counts-with-link until it fits. It never truncates mid-line: each collapse
step drops whole list items or whole sections.

## The JSON summary block

A downstream fixer agent reads the fenced block instead of scraping markdown:

```json
{
  "generated_at": "2026-05-21T07:37:00+00:00",
  "quality_gate": "ERROR",
  "coverage": 19.3,
  "by_severity": {"BLOCKER": 1, "CRITICAL": 12, "MAJOR": 240},
  "blocker_keys": ["AY..."],
  "critical_keys": ["AY...", "AY..."]
}
```

A fixer loop typically:

1. Reads the issue body, slices out the ```json``` fence, `json.loads` it.
2. Iterates `blocker_keys` then `critical_keys` (highest leverage first).
3. For each key, opens the Sonar deep link to read the rule context.
4. Dispatches a fix task; on the next scan the key drops from the lists.

## Run it manually

Through the workflow (uses the repo `SONAR_TOKEN` secret):

```bash
gh workflow run sonar-tracker.yml --repo sipyourdrink-ltd/bernstein
```

Locally, render only (no GitHub write), against your Sonar server:

```bash
SONAR_HOST_URL=https://sonar.example.com \
SONAR_TOKEN=<your-browse-token> \
uv run python scripts/render_sonar_tracker.py --dry-run --output-body /tmp/body.md
```

Locally, render from a saved fixture (no network):

```bash
uv run python scripts/render_sonar_tracker.py \
  --dry-run --fixture tests/unit/sweep/fixtures/issues_search.json \
  --output-body /tmp/body.md
```

## Configuration

| Env var | Purpose |
|---|---|
| `SONAR_HOST_URL` | Sonar server base URL (repo variable). |
| `SONAR_TOKEN` | User token with Browse permission (repo secret). |
| `SONAR_PROJECT_KEY` | Optional; defaults to `bernstein`. |
| `GITHUB_TOKEN` | Used for the `gh issue` create/edit calls. |
| `GITHUB_REPOSITORY` | `owner/name`; defaults from the checkout. |

When `SONAR_TOKEN` is empty (the default on a fork) the workflow logs a
`::notice::` and exits 0, so the tracker is a no-op on forks.

## Health checks

After a run, verify:

1. Exactly one open issue exists with label `sonar-tracker` and the hidden
   marker `<!-- sonar-tracker:bernstein -->` in its body.
2. The issue `updatedAt` changed after the workflow run (a re-run edits the
   same issue rather than opening a new one).
3. The trailing fenced `json` summary parses and reflects current Sonar
   counts.

```bash
gh issue list --repo sipyourdrink-ltd/bernstein --label sonar-tracker --state open
gh issue view <issue-number> --repo sipyourdrink-ltd/bernstein --json body,updatedAt
```

A green run prints a one-line status to the workflow log, for example:

```text
sonar-tracker: updated issue #1234
```

## Troubleshooting

| Symptom | Likely cause | Check | Remediation |
|---|---|---|---|
| Workflow logs a `::notice::` and exits 0 without touching the issue | `SONAR_TOKEN` is empty (expected on forks) | Confirm the `SONAR_TOKEN` secret exists in the target repository settings | Set `SONAR_TOKEN` in the repository; no-op on forks is intended |
| Non-zero exit with `error: sonar fetch failed` | Sonar API unreachable, bad host, or insufficient token scope | Verify `SONAR_HOST_URL`, `SONAR_PROJECT_KEY`, and that `SONAR_TOKEN` has Browse permission on the project | Correct the variable/secret and re-run `workflow_dispatch` |
| Render succeeds but `error: github sync failed` | `GITHUB_TOKEN` lacks `issues: write`, or `GITHUB_REPOSITORY` is wrong | Confirm the job has `issues: write` permission and `GITHUB_REPOSITORY` resolves to `owner/name` | Restore the workflow/job `permissions:` block and retry |
| Two open `sonar-tracker` issues appear | The hidden marker was hand-edited out of an issue body | Run the health-check `gh issue list` above | Close the duplicate; the marker-bearing issue is the canonical one |

Reproduce a fetch failure locally without writing to GitHub:

```bash
SONAR_HOST_URL=https://sonar.example.com \
SONAR_TOKEN=<your-browse-token> \
uv run python scripts/render_sonar_tracker.py --dry-run --output-body /tmp/body.md
```

## Relationship to the other Sonar surfaces

| | Code scanning (`sonar-code-scanning.yml`) | Tracker (`sonar-tracker.yml`) |
|---|---|---|
| Output | SARIF uploaded to the Security tab | one consolidated GitHub issue |
| Visibility | maintainers (write access and above) | anyone who can read the issue |
| De-dup | on `partialFingerprints.sonarFindingKey` | on the hidden body marker |
| Audience | maintainers triaging security alerts | an agent or operator working a thread |

The two are complementary and can run side by side. Pick code scanning
when finding detail should stay in the maintainer-only Security tab; pick
the tracker when you want a single living issue thread to work top-down.

The local backlog sweep (`bernstein doctor sonar-sweep`, documented in
[`sonar-sweeper.md`](sonar-sweeper.md)) still exists for on-demand triage
into gitignored `.sdd/backlog/` files, but it no longer runs on a
schedule.
