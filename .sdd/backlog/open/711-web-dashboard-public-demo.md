# 711 — Public Web Dashboard Demo Instance

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

Nobody will install a tool to see if it's cool. A live demo running at demo.bernstein.dev where visitors can watch real agents working removes ALL adoption friction. Conductor and Crystal are desktop apps — they can't offer this. A public web demo is our competitive advantage as a CLI/server tool.

## Design

### Public demo instance
- Runs on a cheap VPS (Hetzner, Fly.io, Railway)
- Shows a read-only web dashboard of a Bernstein run
- Cycles through demo tasks every 15 minutes
- Real agents working on a sample codebase (not faked)

### Dashboard features for demo
- Live agent output streams
- Task progress with timing
- Cost counter ticking up
- Git diff of completed work
- "Install bernstein" CTA

### Implementation
- Docker compose with bernstein + sample project
- Cron job restarts demo every 15 min
- Readonly dashboard (no auth needed for viewing)
- GitHub link on every page

## Files to modify

- `docker/demo/` (new — Dockerfile, compose, config)
- `src/bernstein/core/server.py` (readonly mode flag)

## Completion signal

- demo.bernstein.dev shows live agent orchestration
- Auto-restarts demo cycle
- Readonly, no auth needed
