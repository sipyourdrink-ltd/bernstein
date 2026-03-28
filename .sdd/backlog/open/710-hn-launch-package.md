# 710 — HN Show Launch Package

**Role:** docs
**Priority:** 0 (urgent)
**Scope:** small
**Depends on:** #700, #701

## Problem

HN Show is the single highest-ROI launch channel for developer tools. A well-executed Show HN can drive 500-2000 stars in 48 hours. But it requires preparation: the README must be perfect, the demo must work, and the submission must be framed correctly.

## Design

### Pre-launch checklist
- [ ] README has autoplay GIF demo
- [ ] `pipx install bernstein && bernstein init && bernstein -g "task"` works on fresh machine
- [ ] Benchmark data in README (X faster than single agent)
- [ ] Comparison page exists
- [ ] YouTube demo video linked
- [ ] GitHub topics set correctly
- [ ] Description is < 80 chars and compelling

### HN Post
Title: `Show HN: Bernstein – one command, multiple AI coding agents in parallel`

Body format (Show HN is text, not link):
```
Bernstein takes a goal, spawns parallel AI coding agents (Claude Code,
Codex, Gemini, Aider), verifies the output with tests, and commits
the results. The orchestrator is deterministic Python — zero LLM tokens
on coordination.

Key difference from other multi-agent tools: it's a CLI, not a desktop
app. Runs headless in CI. Has cost budgeting ($5 cap). Works with any
CLI agent.

Benchmark: 3.2x faster than single agent on parallelizable tasks,
at 1.1x the cost.

https://github.com/chernistry/bernstein

Built this because I got tired of babysitting one agent at a time.
Happy to answer questions about the architecture.
```

### Response strategy
- Answer every comment within 6 hours
- Be technical and honest
- Share benchmark methodology when asked
- Link to "The Bernstein Way" for architecture questions

## Files to modify

- `.sdd/decisions/launch-plan.md` (update)
- `docs/launch/hn-post.md` (new)

## Completion signal

- Pre-launch checklist passes
- HN post draft reviewed and approved
- Response strategy documented
