# E32 — Bitbucket Pipe

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
Bitbucket Pipelines users cannot easily integrate Bernstein without writing custom pipeline scripts and managing dependencies manually.

## Solution
- Create a Bitbucket Pipe at `ci-templates/bitbucket/` with: `Dockerfile` (slim Python image with bernstein pre-installed), `pipe.yml` (metadata and variable definitions), `README.md` (usage and configuration).
- Pipe usage: `pipe: bernstein/orchestrate` with variables `GOAL`, `CONFIG_FILE`, `API_KEY`.
- Dockerfile uses multi-stage build to keep image small.
- Add instructions for publishing to the Bitbucket Pipe registry.

## Acceptance
- [ ] Dockerfile builds successfully and contains a working bernstein installation
- [ ] `pipe.yml` defines all required and optional variables
- [ ] Pipe can be referenced in a `bitbucket-pipelines.yml` file
- [ ] README includes a complete usage example
