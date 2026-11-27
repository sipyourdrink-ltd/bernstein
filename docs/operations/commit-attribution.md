# Commit attribution stats

`bernstein report commits` reports git commit counts and line churn grouped by
agent role, so an operator can see which roles (backend, qa, security, ...)
produced how much of a repository's history without hand-parsing `git log`.

## CLI

```bash
bernstein report commits                     # all-time stats for the current repo
bernstein report commits --since 2025-01-01  # date-bounded stats
bernstein report commits --until 2025-06-30  # date-bounded stats
bernstein report commits --repo-dir ../other-repo
bernstein report commits --json              # machine-readable output
```

The top-level `bernstein commit-stats` alias was removed in v4.0.0 (#3472).
`bernstein report commits` is the only spelling.

| Flag | Default | Meaning |
|---|---|---|
| `--since TEXT` | none | Passed straight to `git log --since`. |
| `--until TEXT` | none | Passed straight to `git log --until`. |
| `--repo-dir PATH` | `.` | Repository to run `git log` against. |
| `--json` | off | Print `CommitStatsResult.to_dict()` instead of a Rich table. |

The table has one row per role, plus a totals row for commits, lines added, and
lines deleted. JSON has stable per-role counters under `roles`, top-level
`total_commits`, `total_lines_added`, and `total_lines_deleted` counters, and an
`error` field (`null` on success, otherwise the error message). For example, a
successful report with one role can look like:

```json
{
  "roles": {
    "backend": {"commits": 4, "lines_added": 120, "lines_deleted": 17}
  },
  "total_commits": 4,
  "total_lines_added": 120,
  "total_lines_deleted": 17,
  "error": null
}
```

Each role's `commits` is its commit count; `lines_added` and `lines_deleted`
are its line-churn counts. `total_lines_added` and `total_lines_deleted` sum
churn across roles. The report uses a separate history query for commit counts.
In mixed history, check `total_commits` for the overall count. Versions that
build the role list from line-count rows can omit roles with no such rows and
report an empty result for a selection containing only empty commits. On failure, the JSON result has the same
fields and puts the message in `error`.

## How role is determined

There is no task/role metadata behind this command — it is entirely a git
`author` string heuristic. For every commit, `bernstein` lowercases the
author's `name <email>` string and checks it for one of a fixed set of
keywords:

```
backend, frontend, qa, security, devops, docs, manager, architect
```

The author string is lowercased, then the keywords are checked in the fixed
order shown above; the first matching keyword in that order wins, regardless
of where it appears in the string. For example, `manager backend-bot
<backend@example.com>` maps to `backend`, because `backend` precedes `manager`
in the keyword list. If no keyword matches, the full lowercased author string
is the role label. For example, `Alice Developer <alice@example.com>` maps to
`alice developer <alice@example.com>`.

Practical effect: this only produces meaningful role buckets when agent
commits carry a role-tagged author identity (for example
`backend-agent <backend@bernstein.local>`). A human co-author whose name or
email happens to contain one of the keywords (e.g. a teammate named
"Devora" would not match, but an email containing `devops` would) is
attributed to that role bucket rather than to themselves — the heuristic
does not distinguish humans from agents.

## Data source

The report uses these `git log` queries. Both use the same `--repo-dir`
(default: the current directory) and receive the same `--since` and `--until`
filters when supplied:

1. `git log --numstat --format=%an <%ae> --date=short` — line-level added/
   deleted counts per commit, attributed by author.
2. `git log --format=%an <%ae>` — a plain commit count per author. Versions
   that return an empty report when no line-count rows are found skip this query
   in that case.

Thus the report's date range and repository selection apply to both its line-
churn query and its commit-count query. If `git` is not installed or the
directory is not a repository, the command exits non-zero and prints the
underlying error (or the `error` field, in `--json` mode).

## Also surfaced in `bernstein doctor`

`bernstein doctor` runs the same `collect_commit_stats()` check in the current
working directory with no date filters. The report's `--since`, `--until`, and
`--repo-dir` options do not apply to this check. Its summary starts with
`{total_commits} commits:` and lists comma-separated role entries of the form
`{role}: {commits} commits, +{added}/-{deleted}`. When no role rows are available,
it reports `no commits found in this repository`. On failure it displays the
underlying Git error.

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
  report commits`), `_doctor_check_commit_attribution` (`bernstein doctor`
  integration)
