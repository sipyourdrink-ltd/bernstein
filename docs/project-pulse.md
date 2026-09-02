# Project pulse

A weekly public snapshot of this repository's health, written to answer the question a
prospective contributor has before opening a pull request: will it be looked at? The headline
is the median time from PR opened to merged over the last 30 days, then a link to grabbable issues.

[`.github/workflows/project-pulse.yml`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/.github/workflows/project-pulse.yml)
publishes it every Monday at 09:19 UTC into one rolling issue titled `Project pulse (weekly)`,
labelled `docs` -- created on the first run, edited in place afterwards.
Find it with [`is:issue "Project pulse (weekly)"`](https://github.com/sipyourdrink-ltd/bernstein/issues?q=is%3Aissue+%22Project+pulse+%28weekly%29%22), or as a workflow artifact on any run.

## What it reports

| # | Metric | Definition |
| --- | --- | --- |
| 1 | Median PR merge lag | Median hours from opened to merged, merged PRs of the last 30 days |
| 2 | Merged within 24 h | Share of those PRs whose merge lag was 24 hours or less |
| 3 | Merged PRs by author class | Counts per class: outside contributor, maintainer account, automation account |
| 4 | Distinct outside authors | How many distinct non-bot, non-maintainer accounts had a PR merged in 90 days |
| 5 | Median issue close lag | Median hours from opened to closed, issues closed in the last 30 days |
| 6 | Work you can pick up | Open `up-for-grabs` and `good first issue` counts, and how many are unassigned |
| 7 | Commit volume | Commits landed on `main` in the last 7 days, and days since the last one |
| 8 | Adapters | Registry size, from the same source `bernstein integrations list` enumerates |
| 9 | Latest release | Tag name and publication date of the most recent release |
| 10 | Translated READMEs | How many of the 23 translations are in sync, per `bernstein readme-l10n verify` |

That table is the whole allow-list, restated as a comment block at the top of
`scripts/project_pulse.py`. Every field is an already-public aggregate. Individual
logins, e-mail addresses, per-person rankings, commit-hour histograms and review-comment
attribution are out of scope and must not be added.

## Regenerate locally

`collect` needs `GH_TOKEN` with public read access and fails closed: a failed query
aborts the run and writes nothing. `render` is pure -- identical input, identical bytes.

```bash
uv run python scripts/project_pulse.py collect --repo sipyourdrink-ltd/bernstein --out pulse.json
uv run python scripts/project_pulse.py render pulse.json
```
