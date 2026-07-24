# Commit attribution stats

`bernstein commit-stats` reports git commit counts and line churn grouped by
agent role, so an operator can see which roles (backend, qa, security, ...)
produced how much of a repository's history without hand-parsing `git log`.

## CLI

```bash
bernstein commit-stats                     # all-time stats for the current repo
bernstein commit-stats --since 2025-01-01  # date-bounded stats
bernstein commit-stats --until 2025-06-30  # date-bounded stats
bernstein commit-stats --repo-dir ../other-repo
bernstein commit-stats --json              # machine-readable output
```

| Flag | Default | Meaning |
|---|---|---|
| `--since TEXT` | none | Passed straight to `git log --since`. |
| `--until TEXT` | none | Passed straight to `git log --until`. |
| `--repo-dir PATH` | `.` | Repository to run `git log` against. |
| `--json` | off | Print `CommitStatsResult.to_dict()` instead of a Rich table. |

Output is a table with one row per role, plus a totals row: commit count,
lines added, lines deleted.

## How role is determined

There is no task/role metadata behind this command — it is entirely a git
`author` string heuristic. For every commit, `bernstein` lowercases the
author's `name <email>` string and checks it for one of a fixed set of
keywords:

```
backend, frontend, qa, security, devops, docs, manager, architect
```

The first keyword found in the author string becomes that commit's role. If
none match, the role label is the full lowercased author string (so a human
contributor's commits show up under their own name/email rather than being
dropped).

Practical effect: this only produces meaningful role buckets when agent
commits carry a role-tagged author identity (for example
`backend-agent <backend@bernstein.local>`). A human co-author whose name or
email happens to contain one of the keywords (e.g. a teammate named
"Devora" would not match, but an email containing `devops` would) is
attributed to that role bucket rather than to themselves — the heuristic
does not distinguish humans from agents.

## Data source

Two `git log` invocations against `--repo-dir` (default: the current
directory):

1. `git log --numstat --format=%an <%ae> --date=short` — line-level added/
   deleted counts per commit, attributed by author.
2. `git log --format=%an <%ae>` — a plain commit count per author.

Both respect `--since`/`--until` when given. If `git` is not installed or the
directory is not a repository, the command exits non-zero and prints the
underlying error (or the `error` field, in `--json` mode).

## Also surfaced in `bernstein doctor`

`bernstein doctor` runs the same `collect_commit_stats()` call (unfiltered by
date) as one of its checks, labelled **Commit attribution**. It reports
`{total commits}: {role}: {commits} commits, +{added}/-{deleted}` per role for
info, or the underlying git error if the check fails.

## Limitations

- Role attribution is a keyword match on the author string, not a link to
  Bernstein's actual per-task role assignment — it will misattribute any
  author whose name or email happens to contain a role keyword.
- No de-duplication or merge-commit filtering: line counts come straight from
  `git log --numstat`, so squash/merge history affects the totals the same
  way it affects `git log` itself.

## Source

- `src/bernstein/cli/commit_stats.py` — `collect_commit_stats`,
  `_author_to_role`, `render_commit_stats`
- `src/bernstein/cli/commands/status_cmd.py` — `commit_stats_cmd` (`bernstein
  commit-stats`), `_doctor_check_commit_attribution` (`bernstein doctor`
  integration)
