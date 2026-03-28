# 713 — GitHub Sponsors + Open Collective

**Role:** docs
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

No way for users to financially support the project. GitHub Sponsors and Open Collective are zero-friction for developers who want to give back. Even $100/month in sponsorships is a signal of project health that attracts more users and contributors.

## Design

### GitHub Sponsors
- Set up funding.yml with GitHub Sponsors link
- Create tier structure:
  - $5/mo: supporter badge
  - $25/mo: priority issue response
  - $100/mo: logo in README
  - $500/mo: consulting call + logo

### Open Collective
- Alternative for companies that can't use GitHub Sponsors
- Transparent spending (builds trust)

### FUNDING.yml
```yaml
github: chernistry
open_collective: bernstein
```

## Files to modify

- `.github/FUNDING.yml` (new)
- `README.md` (add sponsor badge)

## Completion signal

- GitHub Sponsors active
- FUNDING.yml in repo
- Sponsor button appears on GitHub repo page
