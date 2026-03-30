# P85 — Community Leaderboard

**Priority:** P4
**Scope:** small (15 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Open-source contributors lack recognition and visibility, reducing motivation to contribute plugins, workflows, and bug fixes to the bernstein ecosystem.

## Solution
- Build a community contributions leaderboard displayed on the bernstein website
- Track metrics: PRs merged, plugins published, workflows shared, issues triaged
- Pull data from GitHub API (PRs, issues) and catalog/marketplace indexes (plugins, workflows)
- Rank contributors by total contribution score with category breakdowns
- Display monthly highlights: top contributor, most impactful PR, most-installed new plugin
- Post monthly highlights summary to Discord community channel via webhook
- Allow contributors to opt out of the leaderboard

## Acceptance
- [ ] Leaderboard page on website showing ranked contributors
- [ ] Tracks PRs merged, plugins published, workflows shared, issues triaged
- [ ] Data sourced from GitHub API and catalog indexes
- [ ] Monthly highlights section with top contributor and most impactful PR
- [ ] Discord webhook posts monthly highlights summary
- [ ] Contributors can opt out of leaderboard visibility
