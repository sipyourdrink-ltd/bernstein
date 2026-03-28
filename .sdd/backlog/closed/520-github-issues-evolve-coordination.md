# 520 — GitHub Issues as evolve coordination layer

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium

## Problem

When --evolve runs, it creates tasks in local .sdd/backlog/. If multiple people
clone and run `bernstein run --evolve` on the same repo, each instance generates
duplicate proposals with no awareness of what others are working on. There's no
shared coordination layer for distributed self-evolution.

## Design

### GitHub Issues integration
- `--evolve --github` flag syncs evolution proposals as GitHub Issues
- Each proposal gets a labeled issue: `bernstein-evolve`, `auto-generated`
- Before generating a new proposal, check existing issues to avoid duplicates
- When an instance claims a proposal, it assigns the issue to itself (bot label)
- On completion, the issue is closed with a link to the PR

### Distributed evolve protocol
1. Instance starts `--evolve` cycle
2. Fetches open `bernstein-evolve` issues from GitHub
3. Filters out claimed/in-progress ones
4. Either claims an existing issue OR generates new proposal + creates issue
5. Works on the task in a branch
6. Creates PR referencing the issue
7. Closes issue on merge

### Deduplication
- Hash proposal title + key terms, store as issue label
- Before creating new issue, search for similar existing ones (GitHub search API)
- Configurable: `evolve.github_sync: true` in bernstein.yaml

### Benefits for community
- Anyone can clone, run `--evolve`, and contribute improvements
- All evolution work is visible on GitHub Issues
- Community can vote on proposals (thumbs up on issues)
- Transparent self-improvement process builds trust

## Files to modify
- `src/bernstein/evolution/loop.py` — GitHub sync hooks
- `src/bernstein/evolution/creative.py` — issue creation for proposals
- New: `src/bernstein/core/github.py` — GitHub API integration (gh CLI or PyGithub)
- `bernstein.yaml` — evolve.github_sync config

## Completion signal
- `bernstein run --evolve --github` creates issues for each proposal
- Two instances don't work on the same proposal
- PRs reference their source issues


---
**completed**: 2026-03-28 11:33:53
**task_id**: b11f63e9c807
**result**: Completed: [RETRY 1] 520 — GitHub Issues as evolve coordination layer
