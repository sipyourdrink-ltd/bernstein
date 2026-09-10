# Project pulse

Median time from pull request opened to merged over the last 30 days: **2.7 h**.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/project-pulse/pulse-dark.svg?v=2026-09-10">
  <img alt="Project pulse for sipyourdrink-ltd/bernstein, 2026-09-10: median merge lag 2.7 h, 89.8% merged within 24 hours, 1170 merged pull requests in 30 days, 33 unassigned up-for-grabs issues." src="https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/project-pulse/pulse.svg?v=2026-09-10" width="880">
</picture>

> [!TIP]
> [Issues that are free to pick up](https://github.com/sipyourdrink-ltd/bernstein/issues?q=is%3Aissue+is%3Aopen+label%3Aup-for-grabs+no%3Aassignee): 33 unassigned of 35 labelled up-for-grabs, 12 unassigned good first issues.

## Trend

The last 2 weekly collections. The full series is `history.json` on the
`project-pulse` branch.

```mermaid
xychart-beta
    title "Merged pull requests per week"
    x-axis ["09-07", "09-10"]
    y-axis "Merged" 0 --> 2000
    bar [1156, 1170]
```

```mermaid
xychart-beta
    title "Median merge lag, hours"
    x-axis ["09-07", "09-10"]
    y-axis "Hours" 0 --> 3
    line [2.6, 2.7]
```

## Review and merge

| Metric | Value |
| --- | --- |
| Median PR merge lag (30 d) | 2.7 h |
| Merged within 24 h (30 d) | 89.8% |
| Merged PRs (30 d) | 1170 |
| Median issue open to close (30 d) | 24.3 h |
| Issues closed (30 d) | 694 |

## Who merges what

Counts only, by account class. Outside contributions are everything that is neither the
maintainer account nor an automation account, so the outside number is never flattered.

```mermaid
pie showData
    title Merged pull requests by author class, 30 d
    "Outside contributors" : 409
    "Maintainer" : 512
    "Automation" : 249
```

Distinct outside authors with a merged PR in the last 90 days: **80**.

## Work you can pick up

| Label | Open | Unassigned |
| --- | --- | --- |
| up-for-grabs | 35 | 33 |
| good first issue | 12 | 12 |

## Project state

| Metric | Value |
| --- | --- |
| Commits to main (7 d) | 219 |
| Days since last commit | 0 |
| Adapters in the registry | 54 wired in, 52 selectable |
| Latest release | v3.19.2 (2026-09-10) |
| Translated READMEs | 23 in sync, 0 stale, of 23 |

<details>
<summary>How this page is made</summary>

Generated 2026-09-10 from the public GitHub API and this repository's own tree.
Aggregates only: no individual logins, no per-person ranking, no data that is not already public.
The card, the charts and the weekly history on the `project-pulse` branch are projections of the
same ten fields. Regenerate with `scripts/project_pulse.py`; the field allow-list is documented at
the top of that file and in [docs/project-pulse.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/docs/project-pulse.md).

</details>
