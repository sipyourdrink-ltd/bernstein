# 525 — Community-driven evolution: GitHub Issues -> evolve pipeline

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** #520

## Problem

Self-evolution is currently solo — Bernstein improves itself based on internal
metrics. But the killer feature for adoption would be: community members file
issues, and Bernstein evolves to address them automatically. This is the
"self-improving open source project" narrative that gets attention.

## Design

### Community issue flow
1. User files GitHub Issue with label `feature-request` or `evolve-candidate`
2. Bernstein's evolve cycle scans for these issues
3. Visionary agent evaluates: is this feasible? aligned with vision?
4. If approved, creates internal task from issue context
5. Agent works on implementation in a branch
6. PR is created, referencing the original issue
7. Issue is updated with status: "Bernstein is working on this"
8. On merge, issue auto-closes

### Community voting
- Issues with more thumbs-up get higher priority in evolve queue
- `bernstein evolve --community` mode prioritizes community issues
- Dashboard shows: "Community requested -> Bernstein built -> Merged"

### Trust & safety
- Only issues from repo collaborators or with maintainer approval
- Evolve agent cannot modify safety-critical files (existing hash-lock)
- All changes go through PR review (existing L2+ gate)

### Narrative value
- "The first open-source project that implements its own feature requests"
- README badge: "X features built by Bernstein itself from community issues"
- This is the #1 viral differentiator vs CrewAI/AutoGen

## Files to modify
- `src/bernstein/evolution/loop.py` — community issue scanning
- `src/bernstein/evolution/creative.py` — issue-to-proposal conversion
- New: `src/bernstein/core/github.py` — shared GitHub API module
- `.github/ISSUE_TEMPLATE/evolve-candidate.md` — template for community requests

## Completion signal
- Community files issue -> Bernstein creates PR -> PR merged -> issue closed
- End-to-end demonstrated on at least 3 real issues
